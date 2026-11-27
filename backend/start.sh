#!/bin/sh
# Container entrypoint: run pending migrations, then start Gunicorn.
#
# Why this is a script and not a one-liner in CMD:
#
# The previous CMD was
#     alembic upgrade heads || echo '...failed — starting anyway'; exec gunicorn ...
#
# That `||` exists for a good reason. An earlier version used `&&`, so when the
# Supabase pooler dropped connections at deploy time alembic exited non-zero,
# gunicorn never bound the port, and Cloud Run failed the whole revision — turning
# a brief database blip into a hard outage that also blocked rolling out a fix.
# Always starting the server is the right call and is preserved here.
#
# What it got wrong was staying quiet. A *real* migration error — not a blip — was
# reported as one grey line and the app then served requests against the old
# schema. That is the shape of the July incident: new code, old columns,
# InFailedSqlTransaction cascading through every request.
#
# So: retry transient failures, and if migrations genuinely cannot be applied, start
# anyway but say so unmissably, and record it where a human will look.
set -u

MIGRATION_ATTEMPTS="${MIGRATION_ATTEMPTS:-5}"
MIGRATION_RETRY_DELAY="${MIGRATION_RETRY_DELAY:-4}"
STATE_FILE="/tmp/zt_migration_state"

before="$(alembic current 2>/dev/null | tr -d '\r' | head -1)"
echo "[startup] alembic before: ${before:-unknown}"

attempt=1
migrated="no"
while [ "$attempt" -le "$MIGRATION_ATTEMPTS" ]; do
  echo "[startup] alembic upgrade heads (attempt ${attempt}/${MIGRATION_ATTEMPTS})"
  if alembic upgrade heads; then
    migrated="yes"
    break
  fi
  if [ "$attempt" -lt "$MIGRATION_ATTEMPTS" ]; then
    echo "[startup] migration attempt ${attempt} failed — retrying in ${MIGRATION_RETRY_DELAY}s"
    sleep "$MIGRATION_RETRY_DELAY"
  fi
  attempt=$((attempt + 1))
done

after="$(alembic current 2>/dev/null | tr -d '\r' | head -1)"

if [ "$migrated" = "yes" ]; then
  echo "MIGRATIONS_OK" > "$STATE_FILE" 2>/dev/null || true
  echo "[startup] alembic after : ${after:-unknown}"
  echo "[startup] migrations applied successfully"
else
  # Loud, greppable, and impossible to mistake for a routine line. Cloud Run log
  # alerts can match on MIGRATION_FAILURE.
  echo "MIGRATION_FAILURE" > "$STATE_FILE" 2>/dev/null || true
  echo "=============================================================="
  echo "  !!  MIGRATION_FAILURE  !!"
  echo "  Alembic could not reach 'heads' after ${MIGRATION_ATTEMPTS} attempts."
  echo "  The application is starting ANYWAY so the container stays"
  echo "  healthy and can be rolled back or fixed."
  echo ""
  echo "  Schema version is ${after:-unknown}, expected heads."
  echo "  New code may be running against an OLD schema."
  echo "  Check billing and any recently migrated feature before"
  echo "  trusting this revision."
  echo "=============================================================="
fi

exec gunicorn -k uvicorn.workers.UvicornWorker main:app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 2 \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --max-requests 500 \
  --max-requests-jitter 50
