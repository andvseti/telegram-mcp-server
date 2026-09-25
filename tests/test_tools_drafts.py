import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_mcp_server import server


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(server, "_get_client", AsyncMock(return_value=client))


def _patch_entity(monkeypatch):
    monkeypatch.setattr(
        server, "_resolve_entity", AsyncMock(return_value="ENT")
    )


def test_save_draft_ok(monkeypatch):
    client = MagicMock()
    draft = MagicMock()
    draft.set_message = AsyncMock(return_value=True)
    client.get_drafts = AsyncMock(return_value=draft)
    _patch_client(monkeypatch, client)
    _patch_entity(monkeypatch)

    out = asyncio.run(server.save_draft(414634819, "hello draft"))
    assert out["draft_saved"] is True
    assert out["cleared"] is False
    client.get_drafts.assert_awaited_with("ENT")
    draft.set_message.assert_awaited_with("hello draft")


def test_save_draft_empty_text_clears(monkeypatch):
    client = MagicMock()
    draft = MagicMock()
    draft.set_message = AsyncMock(return_value=True)
    client.get_drafts = AsyncMock(return_value=draft)
    _patch_client(monkeypatch, client)
    _patch_entity(monkeypatch)

    out = asyncio.run(server.save_draft(414634819, ""))
    assert out["draft_saved"] is False
    assert out["cleared"] is True
    draft.set_message.assert_awaited_with("")


def test_clear_draft_ok(monkeypatch):
    client = MagicMock()
    draft = MagicMock()
    draft.delete = AsyncMock(return_value=True)
    client.get_drafts = AsyncMock(return_value=draft)
    _patch_client(monkeypatch, client)
    _patch_entity(monkeypatch)

    out = asyncio.run(server.clear_draft(414634819))
    assert out["draft_cleared"] is True
    draft.delete.assert_awaited_once()


def test_save_draft_no_draft_object(monkeypatch):
    # get_drafts returns a Draft even for chats without a draft (empty one),
    # but guard against None / falsy anyway.
    client = MagicMock()
    client.get_drafts = AsyncMock(return_value=None)
    _patch_client(monkeypatch, client)
    _patch_entity(monkeypatch)

    with pytest.raises(server.TelegramMCPError):
        asyncio.run(server.save_draft(414634819, "text"))
