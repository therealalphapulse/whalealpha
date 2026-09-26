"""
hoodalpha/

A completely separate Telegram bot + background service that lives in
this repository purely for deployment convenience (one place to build
from, same installed dependencies), but shares NO runtime state with
WhaleAlpha:

  - Its own Telegram bot token (HOOD_BOT_TOKEN) and its own aiogram
    Bot/Dispatcher instances (hoodalpha/telegram.py, hoodalpha/gateway.py).
  - Its own Postgres schema (`hoodalpha`, created by
    hoodalpha/bootstrap.py) inside the SAME Postgres instance WhaleAlpha
    uses -- HoodAlpha only ever WRITES inside that schema.
  - Its own Redis leader-election locks (hoodalpha/locks.py -- a
    deliberate copy of infra/locks.py's algorithm, not an import of it,
    so the two services have zero Python import coupling).
  - Its own Railway services, deploy commands, and env vars (HOOD_*).

The ONLY coupling to WhaleAlpha is a read-only SQL SELECT against
WhaleAlpha's `public.signal_tokens` table (see hoodalpha/db.py). Nothing
in this package imports from `models/`, `domain/`, `app_platform/`,
`workers/`, `infra/`, or `config/` -- if any of those files change or
are refactored, this package is unaffected as long as the
`signal_tokens` table's column names stay the same.

None of this is wired into main.py, docker-compose.yml, the Procfile,
config/settings.py, or models/__init__.py -- nothing existing was
changed to add this package. See hoodalpha/README.md for env vars and
Railway deployment instructions.
"""
