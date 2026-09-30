# Moovidex MTProto — Production Autofilter Bot

A from-scratch rewrite of the Moovidex autofilter bot on **pure MTProto**
(Pyrogram). No Bot API library, no forwarding hacks, no user session.

## Architecture

```
Telegram  ◄──MTProto──►  Bot client (bot token) — ONE client, everything
                             • search, buttons, inline mode
                             • file delivery (send_document)
                             • web-player streaming (/dl via GetFile,
                               CDN redirects followed + verified)
                             • live auto-index of new channel posts
                             • historical /index backfill (~100+/sec)
```

**One client is enough.** The bot reads channel history itself via
`messages.getHistory` — this works when the bot is **admin** of the
channel (same as Tech VJ-style bots; verified from their source).
No `TG_SESSION`, no second login, no forwarding.

## Features

- 🔍 Smart search (pg_trgm 4-stage pipeline, quality/language/year/S01E01 filters)
- 🎬 Movie cards with TMDB posters, paginated results
- 📥 Instant delivery via MTProto `send_document`
- 🎥 Web player (`/watch`) + `/dl` with **HTTP Range seeking — any file size**
  (Bot API downloads cap at 20 MB; MTProto has no cap; CDN-hosted files
  are followed, AES-decrypted and hash-verified)
- 📥 `/index` — historical backfill at ~100+ files/sec on the bot client.
  Send `/index` with no arguments for the interactive button setup
  (forward a message from the channel, or send its link / @username / id,
  then tweak skip/limit/from/to and hit Start), or one-shot:
  `/index @chan`, `skip=`, `from=`, `to=`, `limit=`, `/index cancel`,
  live progress bar, 🛑 inline stop button, PostgreSQL checkpoints (resume)
- 🤖 Live auto-index — new posts in channels where the bot is admin
- 📢 Force-subscribe (multi-channel), 🔤 spell suggestions, 📊 trending
- 🎞 `/request` movie requests with admin approve/reject
- ⚙️ Admin: `/stats` `/users` `/ban` `/unban` `/broadcast` `/requests` `/settings`
- 🔎 Inline mode (`@yourbot movie name`) in any chat
- 🛡 Optional forward-protection + auto-delete of delivered files

## Quick start (Render)

1. Create a PostgreSQL database (any provider).
2. Get `TG_API_ID` / `TG_API_HASH` from https://my.telegram.org.
3. Push this folder to GitHub, create a Render **Web Service**:
   - Build: `pip install -r requirements.txt`
   - Start: `alembic upgrade head && python main.py`
4. Set environment variables (see `.env.example`). Required:
   `BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`, `DATABASE_URL`,
   `ADMIN_IDS`, `WEB_URL`, `WEB_SECRET`.
5. Add the bot as **admin** to your source channels (required for both
   live auto-index AND `/index` history backfill).
6. `/index @channel` on a small test channel first, then search + download.

## Commands

| Command | Who | What |
|---|---|---|
| `/start`, `/help`, `/trending` | users | intro, help, trending |
| `/request <name>` | users | ask for a missing movie |
| `/index @ch [skip=] [from=] [to=] [limit=]` | admin | historical backfill (bot must be admin) |
| `/index cancel` | admin | stop running job |
| `/stats`, `/users` | admin | stats |
| `/ban`, `/unban <id>` | admin | ban management |
| `/broadcast` (reply) | admin | message all users |
| `/requests` | admin | approve/reject movie requests |
| `/settings` | admin | runtime status |

## Notes

- The process must stay alive (Render paid / always-on). MTProto keeps a
  persistent connection — unlike webhooks it can't cold-start per request.
- File delivery and streaming use the bot client's session; keep the bot a
  member of source channels so `file_reference`s stay valid. Expired
  references are auto-refreshed from the source message once, then fail
  loudly instead of looping.
- `migrations`: single squashed `0001` — fresh databases only. Migrating
  data from the old bot schema is out of scope for this rewrite.
