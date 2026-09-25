"""Telegram MCP server.

Exposes Telegram (via a user account / MTProto through Telethon) as MCP tools:
list chats, read messages, download media, and send messages/files.
All access uses a persistent, lazily-connected TelegramClient.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP
from telethon import TelegramClient
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("telegram-mcp")

mcp = FastMCP("telegram")


class TelegramMCPError(Exception):
    """Raised for expected, user-facing errors (bad args, no session, no access)."""


def _load_config() -> dict[str, Optional[str]]:
    """Read server configuration from the environment."""
    home = Path.home()
    return {
        "api_id": os.environ.get("TELEGRAM_API_ID"),
        "api_hash": os.environ.get("TELEGRAM_API_HASH"),
        "session": os.environ.get("TELEGRAM_SESSION")
        or str(home / ".telegram-mcp" / "telegram.session"),
        "download_dir": os.environ.get("TELEGRAM_DOWNLOAD_DIR")
        or str(home / "Downloads" / "telegram-mcp"),
    }


_UMLAUT_MAP = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _slugify_chat(title: Optional[str]) -> str:
    """Slugify a chat title for use in media filenames."""
    t = (title or "").lower()
    for k, v in _UMLAUT_MAP.items():
        t = t.replace(k, v)
    # Strip remaining diacritics (e.g. "café" -> "cafe") without over-transliterating
    # the German umlauts already handled above.
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"[\s_-]+", "-", t).strip("-")
    return t or "chat"


def _build_media_filename(chat_slug: str, message_id: int, original_name: Optional[str]) -> str:
    """Collision-light filename: <chat-slug>_<message-id>[.<ext>]."""
    base = f"{chat_slug}_{message_id}"
    if original_name:
        ext = original_name.rpartition(".")[2]
        if ext and ext != original_name and re.fullmatch(r"[A-Za-z0-9]+", ext):
            return f"{base}.{ext.lower()}"
    return base


def _telethon_original_name(msg: Any) -> Optional[str]:
    """Extract the original file name from a message's document, if any."""
    doc = getattr(msg, "document", None)
    if doc is None:
        return None
    for attr in getattr(doc, "attributes", None) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return None


def _media_kind(msg: Any) -> Optional[str]:
    """Classify a message's media as photo/video/document/other, or None."""
    if getattr(msg, "photo", None) is not None:
        return "photo"
    doc = getattr(msg, "document", None)
    if doc is not None:
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("video/"):
            return "video"
        return "document"
    if getattr(msg, "media", None) is not None:
        return "other"
    return None


def _format_message(msg: Any) -> dict[str, Any]:
    """Serialize a Telethon message into a plain dict."""
    kind = _media_kind(msg)
    date = getattr(msg, "date", None)
    return {
        "id": msg.id,
        "chat_id": getattr(msg, "chat_id", None),
        "date": date.isoformat() if date is not None else None,
        "sender_id": getattr(msg, "sender_id", None),
        "text": getattr(msg, "message", "") or "",
        "has_media": kind is not None,
        "media_type": kind,
    }


def _parse_chat_ref(chat: Any) -> tuple[str, Any]:
    """Classify a chat reference as ('id', int) / ('username', str) / ('title', str)."""
    if isinstance(chat, bool):
        raise TelegramMCPError("chat must be an id, @username, or title — not a bool")
    if isinstance(chat, int):
        return ("id", chat)
    s = str(chat).strip()
    if not s:
        raise TelegramMCPError("chat must not be empty")
    if s.lstrip("-").isdigit():
        # Looks like a numeric chat id. Accept a single optional leading '-';
        # a malformed variant like "--123" is a bad id, not a title.
        if re.fullmatch(r"-?\d+", s):
            return ("id", int(s))
        raise TelegramMCPError(f"malformed chat id: {s!r}")
    if s.startswith("@"):
        return ("username", s)
    return ("title", s)


def _validate_limit(limit: Any, maximum: int = 200) -> int:
    """Validate a positive-int limit and cap it at `maximum`."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise TelegramMCPError("limit must be a positive integer")
    return min(limit, maximum)


def _dialog_type(dialog: Any) -> str:
    """Map a Telethon dialog to 'group' / 'channel' / 'user' / 'unknown'."""
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    if getattr(dialog, "is_user", False):
        return "user"
    return "unknown"


_client: Optional["TelegramClient"] = None


async def _get_client() -> "TelegramClient":
    """Return a connected, authorized TelegramClient (lazy, cached).

    Assumes the serialized single-event-loop model of a stdio MCP server:
    tool calls are handled one at a time, so the create-and-cache section is
    intentionally unlocked. Authorization is only re-checked on (re)connect.
    """
    global _client
    if _client is not None and _client.is_connected():
        return _client
    cfg = _load_config()
    if not cfg["api_id"] or not cfg["api_hash"]:
        raise TelegramMCPError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set — see README setup."
        )
    try:
        api_id_int = int(cfg["api_id"])
    except (TypeError, ValueError):
        raise TelegramMCPError(f"TELEGRAM_API_ID must be numeric, got {cfg['api_id']!r}")
    session_path = Path(cfg["session"])
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session_path),
        api_id_int,
        cfg["api_hash"],
        flood_sleep_threshold=0,  # surface FloodWaitError instead of silently sleeping
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TelegramMCPError("No valid session — run login.py first.")
    _client = client
    return _client


async def _resolve_entity(client: Any, chat: Any) -> Any:
    """Resolve a chat reference to a Telethon entity.

    id/username go straight to get_entity. Titles are matched against the
    dialog list (exact case-insensitive first, then substring); ambiguous or
    missing titles raise a clear error instead of guessing.

    Note: title resolution fetches the full dialog list (comparatively
    expensive); pass a chat id or @username to skip it.
    """
    kind, val = _parse_chat_ref(chat)
    if kind in ("id", "username"):
        return await client.get_entity(val)
    dialogs = await client.get_dialogs()
    val_l = val.lower()
    exact = [d for d in dialogs if (d.name or "").lower() == val_l]
    matches = exact or [d for d in dialogs if val_l in (d.name or "").lower()]
    if not matches:
        raise TelegramMCPError(f"No chat matching title '{val}'.")
    if len(matches) > 1:
        names = ", ".join(sorted({d.name or "" for d in matches}))
        raise TelegramMCPError(f"Ambiguous chat title '{val}' — matches: {names}")
    return matches[0].entity


# --- Read tools --------------------------------------------------------------

@mcp.tool()
async def get_me() -> dict[str, Any]:
    """Return the logged-in Telegram account (id, username, first_name, phone).

    Doubles as a session check — errors if no valid session exists.
    """
    client = await _get_client()
    me = await client.get_me()
    return {
        "id": me.id,
        "username": getattr(me, "username", None),
        "first_name": getattr(me, "first_name", None),
        "phone": getattr(me, "phone", None),
    }


@mcp.tool()
async def list_chats(query: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """List chats (groups, channels, direct messages).

    Args:
        query: Optional case-insensitive substring filter on the chat title.
            When set, ALL dialogs are scanned so a match beyond the most-recent
            `limit` chats is still found (comparatively expensive, like title
            resolution); `limit` then caps the number of matches returned.
        limit: Max results (default 50, capped at 500). Without `query` this is
            also how many dialogs are fetched.

    Returns objects with id, title, type ('group'/'channel'/'user'), unread.
    Use this to find a chat's id/title before calling other tools.
    """
    limit = _validate_limit(limit, maximum=500)
    client = await _get_client()
    dialogs = await client.get_dialogs(limit=None if query else limit)
    out: list[dict[str, Any]] = []
    for d in dialogs:
        name = d.name or ""
        if query and query.lower() not in name.lower():
            continue
        out.append(
            {
                "id": d.id,
                "title": name,
                "type": _dialog_type(d),
                "unread": getattr(d, "unread_count", 0),
            }
        )
        if len(out) >= limit:
            break
    return out


@mcp.tool()
async def get_messages(
    chat: Any,
    limit: int = 30,
    before_id: Optional[int] = None,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read recent messages from one chat.

    Args:
        chat: Chat id, @username, or exact/substring title.
        limit: Max messages (default 30, capped at 200).
        before_id: Only messages older than this message id (pagination).
        search: Optional full-text filter within the chat.

    Returns message dicts (id, chat_id, date, sender_id, text, has_media, media_type).
    """
    limit = _validate_limit(limit)
    client = await _get_client()
    entity = await _resolve_entity(client, chat)
    kwargs: dict[str, Any] = {"limit": limit}
    if before_id is not None:
        kwargs["offset_id"] = before_id
    if search is not None:
        kwargs["search"] = search
    msgs = await client.get_messages(entity, **kwargs)
    return [_format_message(m) for m in msgs if m is not None]


async def _search_global(client: Any, query: str, limit: int) -> list[Any]:
    """Search messages across all chats via SearchGlobalRequest."""
    result = await client(
        SearchGlobalRequest(
            q=query,
            filter=InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=limit,
        )
    )
    return list(result.messages)


@mcp.tool()
async def search_messages(
    query: str,
    chat: Optional[Any] = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Search messages by text — globally or within one chat.

    Args:
        query: Non-empty search string.
        chat: Optional chat id/@username/title to scope the search.
        limit: Max results (default 30, capped at 200).

    Returns message dicts (id, chat_id, date, sender_id, text, has_media, media_type).
    """
    if not query or not query.strip():
        raise TelegramMCPError("query must not be empty")
    limit = _validate_limit(limit)
    client = await _get_client()
    if chat is not None:
        entity = await _resolve_entity(client, chat)
        msgs = await client.get_messages(entity, limit=limit, search=query)
    else:
        msgs = await _search_global(client, query, limit)
    return [_format_message(m) for m in msgs if m is not None]


# --- Media tools ---------------------------------------------------------------

_MEDIA_KINDS = {"photo", "video", "document", "other"}


def _entity_slug(entity: Any, chat: Any) -> str:
    """Best-effort slug for filenames from an entity's title/username."""
    title = getattr(entity, "title", None) or getattr(entity, "username", None)
    if not title:
        title = str(_parse_chat_ref(chat)[1])
    return _slugify_chat(title)


async def _prepare_download(client: Any, chat: Any, dest_dir: Optional[str]) -> tuple[Any, str, Path]:
    """Resolve the chat, derive a filename slug, and ensure an absolute target dir."""
    entity = await _resolve_entity(client, chat)
    slug = _entity_slug(entity, chat)
    target = Path(dest_dir or _load_config()["download_dir"]).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return entity, slug, target


async def _download_one(client: Any, msg: Any, slug: str, target: Path) -> Optional[str]:
    """Download one message's media; returns the absolute path, or None if Telethon
    could not download this media type (poll, geo, story, unsupported, ...)."""
    fname = _build_media_filename(slug, msg.id, _telethon_original_name(msg))
    return await client.download_media(msg, file=str(target / fname))


@mcp.tool()
async def download_media(
    chat: Any,
    message_ids: list[int],
    dest_dir: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Download media from specific messages to disk.

    Args:
        chat: Chat id/@username/title.
        message_ids: Message ids whose media should be downloaded.
        dest_dir: Target directory (default TELEGRAM_DOWNLOAD_DIR).

    Returns [{message_id, path}] for messages whose media downloaded
    successfully; non-media and undownloadable media are skipped. Paths are
    absolute.
    """
    if not message_ids:
        raise TelegramMCPError("message_ids must not be empty")
    client = await _get_client()
    entity, slug, target = await _prepare_download(client, chat, dest_dir)
    msgs = await client.get_messages(entity, ids=message_ids)
    results: list[dict[str, Any]] = []
    for m in msgs:
        if m is None or getattr(m, "media", None) is None:
            continue
        path = await _download_one(client, m, slug, target)
        if path is None:
            continue
        results.append({"message_id": m.id, "path": path})
    return results


@mcp.tool()
async def get_recent_media(
    chat: Any,
    limit: int = 20,
    kind: Optional[str] = None,
    dest_dir: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Download the most recent media items from a chat.

    Args:
        chat: Chat id/@username/title.
        limit: How many recent messages to scan (default 20, capped at 200).
        kind: Optional filter — 'photo' / 'video' / 'document' / 'other'.
        dest_dir: Target directory (default TELEGRAM_DOWNLOAD_DIR).

    Returns [{message_id, kind, path}] for successfully downloaded media. This
    is the fast path for pulling images out of a group (e.g. the Arcanara
    Juni-Treffen photos). Paths are absolute.
    """
    if kind is not None and kind not in _MEDIA_KINDS:
        raise TelegramMCPError(f"kind must be one of {sorted(_MEDIA_KINDS)} or None")
    limit = _validate_limit(limit)
    client = await _get_client()
    entity, slug, target = await _prepare_download(client, chat, dest_dir)
    msgs = await client.get_messages(entity, limit=limit)
    results: list[dict[str, Any]] = []
    for m in msgs:
        if m is None or getattr(m, "media", None) is None:
            continue
        mk = _media_kind(m)
        if kind and mk != kind:
            continue
        path = await _download_one(client, m, slug, target)
        if path is None:
            continue
        results.append({"message_id": m.id, "kind": mk, "path": path})
    return results


# --- Write tools -------------------------------------------------------------

@mcp.tool()
async def send_message(chat: Any, text: str) -> dict[str, Any]:
    """Send a text message to a chat as the logged-in user.

    This is a real, irreversible send, visible to the chat's members, and
    cannot be recalled via this API — confirm the chat and text before calling.

    Args:
        chat: Chat id/@username/title.
        text: Non-empty message text.

    Returns {"sent": True, "message_id": <id>}.
    """
    if not text or not text.strip():
        raise TelegramMCPError("text must not be empty")
    client = await _get_client()
    entity = await _resolve_entity(client, chat)
    sent = await client.send_message(entity, text)
    return {"sent": True, "message_id": sent.id}


@mcp.tool()
async def send_file(chat: Any, file_path: str, caption: Optional[str] = None) -> dict[str, Any]:
    """Send a local file (image/document) to a chat as the logged-in user.

    This is a real, irreversible send, visible to the chat's members, and
    cannot be recalled via this API — confirm the chat and file before calling.

    Args:
        chat: Chat id/@username/title.
        file_path: Absolute path to an existing local file.
        caption: Optional caption text.

    Returns {"sent": True, "message_id": <id>}.
    """
    if not Path(file_path).is_file():
        raise TelegramMCPError(f"file not found: {file_path}")
    client = await _get_client()
    entity = await _resolve_entity(client, chat)
    sent = await client.send_file(entity, file_path, caption=caption)
    return {"sent": True, "message_id": sent.id}


@mcp.tool()
async def save_draft(chat: Any, text: str) -> dict[str, Any]:
    """Save a message draft in a chat without sending it.

    The draft appears in the target chat as an unsent message (and as a
    "Draft" label in the chat list) for the logged-in account, on all its
    clients. It overwrites any existing draft in that chat.

    Text formatting (Telethon markdown):
        **bold**, __italic__, `inline code`,
        ```code blocks```, [link text](https://url), plain unicode bullets.
        Note: single *asterisk* italics are NOT parsed — use __underscores__.

    Args:
        chat: Chat id (preferred), @username or exact title.
        text: Draft text; empty string clears the draft.

    Returns {"draft_saved": bool, "chat": <title>, "cleared": bool}.
    """
    client = await _get_client()
    entity = await _resolve_entity(client, chat)
    draft = await client.get_drafts(entity)
    if draft is None:
        raise TelegramMCPError(f"no draft object returned for chat {chat!r}")
    await draft.set_message(text or "")
    return {
        "draft_saved": bool(text and text.strip()),
        "chat": getattr(entity, "title", None) or getattr(entity, "username", None),
        "cleared": not (text and text.strip()),
    }


@mcp.tool()
async def clear_draft(chat: Any) -> dict[str, Any]:
    """Clear (remove) a previously saved draft in a chat.

    Args:
        chat: Chat id/@username/title.

    Returns {"draft_cleared": True, "chat": <title>}.
    """
    client = await _get_client()
    entity = await _resolve_entity(client, chat)
    draft = await client.get_drafts(entity)
    await draft.delete()
    return {
        "draft_cleared": True,
        "chat": getattr(entity, "title", None) or getattr(entity, "username", None),
    }


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
