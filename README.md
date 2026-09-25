# telegram-mcp-server

MCP-Server für Telegram über einen **User-Account** (MTProto/Telethon).
Liest Chats/Nachrichten/Medien und sendet Nachrichten/Dateien. Läuft lokal über stdio.

## Warum User-Account statt Bot?

Ein Bot kann keine Gruppen-*Historie* und keine vor seinem Beitritt geposteten
Medien lesen (und hat ein 20-MB-Download-Limit). Für „hol mir die Bilder aus der
Gruppe" braucht es einen User-Client. Session-Datei = voller Account-Zugriff →
lokal halten, `chmod 600`, nie committen.

## Setup

1. API-Credentials auf https://my.telegram.org (API development tools) erzeugen.
2. Repo installieren (venv + editierbar; Python ≥3.10):
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```
3. Environment setzen:
   ```bash
   export TELEGRAM_API_ID=...
   export TELEGRAM_API_HASH=...
   export TELEGRAM_PHONE=+49...        # nur für den Login nötig
   # optional:
   export TELEGRAM_SESSION="$HOME/.telegram-mcp/telegram.session"
   export TELEGRAM_DOWNLOAD_DIR="$HOME/Downloads/telegram-mcp"
   ```
4. Einmalig einloggen (erzeugt die Session-Datei, fragt Code + ggf. 2FA):
   ```bash
   .venv/bin/python -m telegram_mcp_server.login
   ```

## In Claude Code registrieren

```bash
claude mcp add telegram \
  -e TELEGRAM_API_ID=... \
  -e TELEGRAM_API_HASH=... \
  -e TELEGRAM_SESSION="$HOME/.telegram-mcp/telegram.session" \
  -- /ABSOLUTER/PFAD/telegram-mcp-server/.venv/bin/telegram-mcp-server
```
Die `-e`-Flags sind nötig, weil ein von Claude Code gestarteter MCP-Server die
Shell-`export`s aus dem Setup NICHT erbt. Alternativ die `env`-Sektion in der
MCP-Konfiguration nutzen. `TELEGRAM_DOWNLOAD_DIR` optional per weiterem `-e` setzen.
Nach dem Registrieren eine neue Claude-Code-Session starten, damit der Server erscheint.

## Tools

| Tool | Zweck |
|---|---|
| `get_me` | Whoami / Session-Check |
| `list_chats(query?, limit)` | Chats auflisten (Gruppe/Kanal/DM) |
| `get_messages(chat, limit, before_id?, search?)` | Nachrichten eines Chats lesen |
| `search_messages(query, chat?, limit)` | Nachrichten suchen (global oder im Chat) |
| `download_media(chat, message_ids[], dest_dir?)` | Medien gezielter Nachrichten laden |
| `get_recent_media(chat, limit, kind?, dest_dir?)` | Letzte N Medien eines Chats laden |
| `send_message(chat, text)` | Textnachricht senden |
| `send_file(chat, file_path, caption?)` | Datei/Bild senden |
| `save_draft(chat, text)` | Nachricht als **Entwurf** im Chat speichern (leerer Text = Entwurf löschen) |
| `clear_draft(chat)` | Gespeicherten Entwurf im Chat entfernen |

`chat` akzeptiert Chat-id, `@username` oder (Teil-)Titel; bei mehrdeutigem Titel
kommt ein Fehler mit den Treffern statt einer Rate-Auflösung.

`get_messages`/`search_messages` liefern pro Nachricht auch `chat_id` mit —
Treffer aus der globalen Suche sind damit direkt in weiteren Tool-Aufrufen
(z.B. `download_media`) verwendbar.

Entwurf-Text supports Telethon-Markdown: `**bold**`, `__italic__`,
`inline code`, ` ```code blocks``` `, `[link](url)`. Einzelne `*Sterne*`
für Kursiv werden NICHT geparst. Der Entwurf erscheint (mit „Entwurf“-Label
in der Chatliste) auf allen Clients des eingeloggten Accounts und überschreibt
eine vorhandene Entwurf im selben Chat.

## Entwicklung / Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest tests/ -v
```
Tests laufen ohne Live-Account (Client wird gemockt).

## Smoke-Test (einmalig nach Login, mit echtem Account)

```bash
.venv/bin/python -m telegram_mcp_server.login        # 1) Session erzeugen
# 2) get_me / list_chats / get_messages / download_media manuell über den
#    MCP-Client (Claude Code) prüfen:
#    - get_me                         -> zeigt deinen Account
#    - list_chats query="<gruppe>"    -> findet eine deiner Gruppen
#    - get_messages chat=<id> limit=5 -> liefert Nachrichten
#    - search_messages query="..."    -> globale Suche (übt _search_global live aus;
#                                         Treffer müssen chat_id tragen)
#    - get_recent_media chat=<id> kind="photo" -> lädt Fotos nach $TELEGRAM_DOWNLOAD_DIR
#    - save_draft chat=<id> text="Test" -> Entwurf erscheint im Chat (und als
#                                         „Entwurf“-Label in der Chatliste);
#                                         clear_draft chat=<id> räumt wieder auf
```
Erster echter Integrationstest = ein Medienabruf aus einer deiner Gruppen. Die globale `search_messages`
(ohne `chat`) ist der einzige Pfad, der `_search_global`/`SearchGlobalRequest` real ausführt —
im Smoke-Test nicht überspringen.

## Maintainer

Schimmi — https://schimmilab.de
Issues und Pull Requests willkommen.
