# EcoGuard Uganda — backend

FastAPI service: cookie authentication + X-CSRF-Token, private evidence,
human-reviewed advisories, Redis cache and a realtime SSE stream
(`GET /api/v1/stream`), plus **CPU SpeciesNet image assistance**. Runs on
PostgreSQL, deployed on Vercel.

Image assistance is a *suggestion*, never a determination: the model names a
candidate species, and only a human sets a report's species. See
[`docs/AI.md`](docs/AI.md) for the model and [`docs/CLASSIFICATION.md`](docs/CLASSIFICATION.md)
for the architecture and its known limitations.

## Local development

The base venv has no ML dependencies, which is fine for everything except running
the classifier:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit for your local Postgres/Redis
uvicorn app.main:app --reload
```

To run the real classifier locally you need Python 3.12 (or 3.13) and the ML
extra — SpeciesNet has no wheels for 3.13+ and `torch`/`torchvision` must be
the `+cpu` builds:

```bash
python3.12 -m venv .venv-ml && . .venv-ml/bin/activate
pip install -r requirements-ml.txt
```

Swagger docs: `http://localhost:8000/api/docs`.

## Local development with Docker

`docker-compose.yml` runs the whole backend — Postgres, Redis and the API — so
no local Python or database install is needed. Postgres is reachable on
`localhost:5432` as `postgres` / `postgres` (database `ecoguard`), which is what
`.env` already expects, so the native venv workflow above keeps working too.

```bash
docker compose up -d --build        # db + redis + api on :8000
docker compose ps                   # all three report healthy
docker compose logs -f api
docker compose down                 # stop, keep data
```

> **`docker compose down -v` will delete the model volume.** The
> `ecoguard_model-data` volume holds ~488 MB of SpeciesNet weights. If it is
> removed, the next prediction re-downloads them and the model reports
> `degraded` until that finishes. Recreate a single service instead:
> `docker compose up -d --build api`.

### Image assistance in Docker

`IMAGE_ASSISTANCE=auto` is the default: assistance is offered when the runtime
and weights are present, and silently skipped otherwise. The API container
loads the model **once per process** in a background thread at startup, which
takes ~30 s on CPU. You can watch it happen:

```bash
docker compose logs -f api | grep '\[AI\]'
```

```
[AI] loading SpeciesNet whole-image classifier: state=loading model=speciesnet-5.0.5-... device=cpu
[AI] SpeciesNet loaded successfully: ... checkpoint=4.0.3b labels=2498 device=cpu load_time=30.73s image_assistance=ready
[AI] warmup finished: loaded=True ... image_assistance=ready
```

Set `IMAGE_ASSISTANCE=disabled` to turn it off; `/api/v1/config` then reports
`not_configured` and no model is loaded. The relevant variables are
`IMAGE_ASSISTANCE`, `SPECIESNET_MODEL`, `SPECIESNET_LOCAL_DIR`,
`SPECIESNET_CACHE_DIR`, `SPECIESNET_DEVICE`, `SPECIESNET_WARMUP`,
`MODEL_VERSION` and `CONFIDENCE_THRESHOLD`.

Weights live in the `model-data` volume mounted at `/app/var/models`, which is
also `KAGGLEHUB_CACHE`. Nothing is downloaded per request.

Tables are created on first start (`AUTO_CREATE_TABLES=true`). The communities
that reports must reference are seeded once with:

```bash
docker compose run --rm -e DEMO_ENABLED=true api \
  python -m app.cli seed-demo --password '<12+ character password>'
```

That prints the `reporter@`, `reviewer@`, `publisher@` and `admin@`
`ecoguard.example.org` accounts that share the password you supplied.

## Administrators

An administrator signs in at `#/staff/login`. Accounts holding the `admin` role
additionally get an **Admin** page in the workspace navigation, which reports account
provisioning, area coverage and delivery health, and warns about the configurations that
make the rest of the product look broken (an area with no assigned staff, staff with no
area, a background job that has stopped retrying). Creating staff and assigning areas is
done in **Team & settings**, which the Admin page links into.

Technical administrator does not imply evidence review or publishing; those are granted
separately. The admin overview is served by `GET /api/v1/admin/dashboard` and is never
cached, so it reflects the state as of the read.

To create an administrator:

```bash
python -m app.cli ensure-admin --email you@example.org --name 'Your Name' \
  --password '<12+ character password>'
```

`ensure-admin` is idempotent, so it is the command to use when an administrator has lost
their password: re-running it resets the password and revokes that account's sessions
instead of failing the way `create-admin` does. `--roles` and `--areas` are optional and
**only change what you pass** — omitted means "leave the existing assignment alone", so a
bare re-run cannot silently strip access. Pass `--areas=` to clear an assignment. Area keys
must already exist, otherwise it refuses rather than creating a dangling reference.

```bash
# grant review rights over two known areas
python -m app.cli ensure-admin --email you@example.org --password '<12+ chars>' \
  --roles admin,reviewer --areas community-a,wetland-a
```

A brand new administrator has no assigned areas, which makes the area-scoped staff map
and dashboard render nothing. Assign at least one, or ask another administrator to do it
through **Team & settings**: `PATCH /admin/users/{id}` refuses self-modification by
design, so a lone administrator cannot grant themselves access.

The frontend runs on the host against this API, proxying `/api` to it:

```bash
cd ../ecoguard-frontend
API_PROXY_TARGET=http://localhost:8000 npm run dev   # http://localhost:5173
```

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q -p no:randomly
```

45 tests, no model weights, no network and no PyTorch required — the classifier
is stubbed. Real-model behaviour is covered separately by
`python scripts/speciesnet_smoke.py <image>`, which needs the ML extra.

## Deployment (Vercel)

Project `ecoguard-api` → FastAPI entrypoint `app/main.py` is auto-detected.
Required environment variables (Vercel project):

- `DATABASE_URL` — Neon Postgres (`postgresql+psycopg://...` with Neon's pooler host, sslmode=require)
- `REDIS_URL` — Upstash Redis (`rediss://...`), used for cache + realtime event log
- `ALLOWED_ORIGINS` — frontend origin e.g. `https://ecoguard.vercel.app`
- `COOKIE_SECURE` = `true`, `SESSION_SAMESITE` = `none` (cross-site session cookie)
- `APP_ENV` = `development`, `AUTO_CREATE_TABLES` = `true` (idempotent create_all on cold start)
- `JOBS_MODE` = `inline` (no Celery worker on serverless)
- `CSRF_REALM` note: keep `COOKIE_SECURE=true` so `SameSite=None` cookies require HTTPS.

Vercel installs `requirements.txt` only, so **image assistance is unavailable
there** and `/api/v1/config` reports `degraded`. This is expected, not a
misconfiguration: the classifier needs ~2 GB of native dependencies that the
serverless runtime does not have. Set `IMAGE_ASSISTANCE=disabled` on Vercel to
make that explicit to the frontend instead of relying on the degraded state.
Prediction requests still succeed as requests — they return `202` with
`state: "unavailable"` and the same "human reporting remains available"
explanation, never an invented species.

Evidence images use the `<your-project>-private` local storage on Vercel and are
ephemeral per function instance; connect S3 credentials
(`STORAGE_BACKEND=s3` + `S3_BUCKET`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`) to persist
uploads.

## Operational notes

- `/health` returns `{"status":"ok","service":...,"version":...}`.
  `/api/v1/health/live` and `/api/v1/health/ready` also exist; `ready` checks the
  database.
- `GET /api/v1/config` reports the live `image_assistance` state
  (`not_configured` / `degraded` / `ready`) and `ai_model_version`. Only the
  deployment's static fields are cached, so a model that finishes loading is
  never masked by a stale `degraded`.
- `degraded` is intentionally broad: it covers *still loading*, *weights or
  runtime absent* and *load failed*. `GET /api/v1/admin/image-assistance`
  (admin only) returns the specific reason.
- `AUTO_CREATE_TABLES` + `APP_ENV=development` run idempotent `create_all` at
  startup; production migration flow is via Alembic if you switch `APP_ENV=production`.
- Application logging is configured by `app/logging_config.py`, which attaches a
  handler to the `app` logger tree only. The root logger is left alone
  deliberately, so third-party libraries do not flood the log. The worker calls
  the same setup in `app/worker.py`.
- Every prediction writes `prediction.requested` and then either
  `prediction.completed` or `prediction.failed` to the `audit` table. The audit
  row references the `Prediction`, which holds the result; intermediate retries
  are not audited.