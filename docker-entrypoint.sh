#!/bin/sh
# docker-entrypoint.sh — single, observable startup pipeline for Railway.
#
# ROOT CAUSE THIS FIXES:
# Previously, nothing in this repository chained "run migrations" to
# "start the application" into one process. The Dockerfile's CMD only ever
# ran `python -m whale_alpha.main`, which never invokes Alembic — yet the
# Railway deploy logs showed Alembic running and then nothing. That is only
# possible if Alembic was being executed as a separate, disconnected step
# (a dashboard-configured Start/Pre-Deploy Command), and whatever ran it
# never went on to launch the bot process — either because that command was
# never chained to the real start command, or because it silently exited
# without the chain continuing and without a non-zero exit Railway would
# have surfaced as a failed deploy.
#
# This script makes that failure mode structurally impossible: migrations
# and app startup are now one PID-1 process tree, defined in version
# control (not a dashboard field), with `set -e` so any failure aborts
# loudly and `exec` so the Python process becomes PID 1 and receives
# SIGTERM directly from Railway on redeploy/shutdown (correct signal
# handling — no orphaned/zombie processes, no silent hangs).
set -e

log() {
    printf '%s [entrypoint] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$1"
}

log "Loading environment..."
if [ -z "$DATABASE_URL" ]; then
    log "FATAL: DATABASE_URL is not set. Refusing to start."
    exit 1
fi
log "Environment loaded."

log "Running Alembic..."
# Run migrations as a foreground, non-backgrounded step so any failure
# (bad revision, unreachable DB, lock timeout) aborts the deploy loudly
# with alembic's own traceback, instead of the container silently idling.
if ! alembic upgrade head; then
    status=$?
    log "FATAL: Alembic migration failed (exit code ${status}). See traceback above."
    exit "$status"
fi
log "Database Ready."

log "Starting application: $*"
# exec replaces this shell with the Python process (PID 1) — required so
# SIGTERM/SIGINT from Railway reach whale_alpha.main directly for graceful
# shutdown, instead of being caught (and swallowed) by an intermediate shell.
exec "$@"
