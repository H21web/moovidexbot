# MooviDex v11.1 — "Bandwidth Saver" release

Built on v10.15.10. Full deployable tree.

## 🐛 v11.1 hotfix (2026-10-15)

- **OTT buttons dead on arrival — FIXED.** The OTT module used
  class-level `@Client.on_*` decorators, which Pyrogram 2.x silently
  ignores outside plugin loading. All 8 handlers now register on the
  bot instance.
- **One-tap download.** Tapping a movie in `/ott`, `/latest`,
  `/today`, `/upcoming` now runs the search **in the current chat**
  and shows the results card with download options — no more
  deep-link round-trip to PM.
- **Stays on the current page.** Detail cards edit the list message
  in place (with 🔙 Back), instead of delete + resend.

## 🌐 Bandwidth fixes

The web player used to proxy **every byte** through Render
(Telegram → Render → user). v11 cuts that hard:

1. **Small-file redirect** — files ≤ 20 MB served by `/dl` are now
   HTTP-302 redirected to Telegram's own file servers
   (`api.telegram.org/file/bot<token>/…`). Zero Render bandwidth,
   falls back to the MTProto proxy if the Bot API can't serve it.
2. **"📲 Get in Telegram" button** on the watch page — one tap
   delivers the file via the bot (server-side, zero bandwidth,
   no buffering).
3. **BANDWIDTH_SAVER mode (default ON)** — the ⬇ Download button on
   delivered files no longer re-downloads through the web server
   (the file is already in the chat!). It now explains that instead.
   Set `BANDWIDTH_SAVER=false` in env to restore the old button.
4. In-Telegram delivery (`send_cached_media`) was already
   zero-bandwidth — it stays the default path everywhere.

## ⚡ Performance

- Group rows hot-cached (60 s) — delivery no longer does a DB
  round-trip per file; cache refreshes instantly on settings save.
- OTT API clients use `trust_env=False` (proxy-env proof).
- Enrich/TMDB caches already in place; OTT lists cached 30 min.

## 🔲 Search results: list ⇄ button view

- New per-user **Results view** toggle: 📋 List (classic) or
  🔲 Buttons (one tappable button per file).
- Toggle lives on every results card + in My Account → 🎨 Preference.
- Stored per user, survives restarts.

## 📺 OTT discovery (integrated from your plugins, bugs fixed)

| Command | What |
|---|---|
| `/ott` | Browse platforms → movies → tap to search (fixed the crash bug) |
| `/latest` | Latest streaming movies, inline pages (no more reply-keyboard spam) |
| `/today` | **New** — movies releasing today |
| `/upcoming` | Coming soon with cinematic cards + 🔔 Notify me |

- `/upcoming` 🔔 Notify me → bot DMs you the moment the title is indexed.
- Bugs fixed from the originals: `/ott` indentation crash, photo-message
  edit crash, `notify_` title-parsing bug, re-fetch on every tap,
  unbounded session dicts (now TTL-pruned), hardcoded bot username.

## 🗄 Deploy steps

1. Replace tree, `pip install -r requirements.txt` (adds **Pillow**).
2. `python -m alembic upgrade head` (adds `movie_alerts` table).
3. Redeploy. No other env changes needed (`BANDWIDTH_SAVER` defaults ON).

## ⚠️ Notes

- Live-test `/ott`, `/latest`, `/today`, `/upcoming` once after deploy
  (binged/ottplay APIs are third-party and can change).
- Web player still streams via the server when users explicitly choose
  it — that's the remaining bandwidth cost, now opt-in per view.
