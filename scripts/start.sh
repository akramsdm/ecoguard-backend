#!/bin/sh
# Container entrypoint for Railway and any host that injects $PORT.
#
# Why this exists instead of a bare uvicorn command:
#   * $PORT is assigned by the platform. A hardcoded --port 8000 would leave the
#     app listening where nothing routes to it.
#   * Migrations run here so the schema is at head *before* the first request is
#     served. Any failure exits non-zero, so the platform restarts and retries
#     rather than serving traffic against a half-migrated schema.
#   * X-Forwarded-For is trusted, otherwise every request appears to come from the
#     platform proxy's IP and the auth rate limiter would lock out all users at once.
#
# Set RUN_MIGRATIONS_ON_START=true to migrate on boot. Leave it false when a
# release step already ran `alembic upgrade head` (or when several replicas start
# simultaneously and you would rather migrate once, separately).
set -e

PORT="${PORT:-8000}"
RUN_MIGRATIONS_ON_START="${RUN_MIGRATIONS_ON_START:-false}"

echo "[start] port=${PORT} run_migrations=${RUN_MIGRATIONS_ON_START} app_env=${APP_ENV:-development}"

if [ "${RUN_MIGRATIONS_ON_START}" = "true" ]; then
  # A freshly provisioned database (or one still restoring a backup) refuses
  # connections for a few seconds after the app container is already running.
  python - <<'PY'
import sys, time
from app.db import engine

deadline = time.time() + 120
while True:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('select 1')
        print('[start] database reachable', flush=True)
        break
    except Exception as exc:
        if time.time() > deadline:
            print(f'[start] database unreachable: {type(exc).__name__}', flush=True)
            sys.exit(1)
        time.sleep(3)
PY
  echo "[start] applying migrations (alembic upgrade head)"
  alembic upgrade head
  echo "[start] schema is at head"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
