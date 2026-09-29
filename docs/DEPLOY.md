# Deployment

The API runs on **Railway** and the web app on **Vercel**. Nothing in either repo
is hardcoded to `localhost` or to a fixed port; everything comes from environment
variables. This document is the checklist for a first deploy and the reference for
changing configuration later.

- Web app (`ecoguard-frontend`): static Vite build, deployed to Vercel.
- API (`ecoguard-backend`): FastAPI in a container, deployed to Railway.

---

## 1. Railway (API)

### 1.1 Build and start

`railway.json` pins the builder to the repo `Dockerfile`:

```json
{ "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" } }
```

The image is built as-is; no adjustment was required for Railway's builder. Two
things inside the Dockerfile are deployment-specific:

- **Start command** — `CMD ["sh", "scripts/start.sh"]`. The script listens on
  `${PORT:-8000}` because Railway assigns the port; a hardcoded `8000` would leave
  the app listening where nothing routes to it. It also passes
  `--proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}"`, without
  which every request would appear to come from Railway's proxy and the auth rate
  limiter would lock out all users at once.
- **`ARG INSTALL_ML`** — set the build variable to `false` to skip
  `requirements-ml.txt` (torch + torchvision CPU ≈ 2 GB) when the deployment runs
  with `IMAGE_ASSISTANCE=disabled`. The classifier is imported lazily, so a build
  without it starts normally and reports `image_assistance: "degraded"`.

### 1.2 Migrations: run on start

**Chosen approach: `alembic upgrade head` runs inside `scripts/start.sh`, gated by
`RUN_MIGRATIONS_ON_START=true`.** No separate release/release-phase step.

Why: the schema is guaranteed to be at head *before* the first request is served,
and it happens on the same deploy that changed the code. The script waits up to
120 s for the database (a newly provisioned instance refuses connections for a few
seconds), then migrates, then `exec`s uvicorn. Any failure exits non-zero and
`railway.json` restarts the container (`ON_FAILURE`, 10 retries) instead of serving
traffic against a half-migrated schema.

Alternatives and when to use them:

- **Railway release phase / pre-deploy command** — set `RUN_MIGRATIONS_ON_START=false`
  and migrate once before the new replicas start. Required if you scale the API to
  more than one replica, because concurrent `alembic upgrade head` calls on the same
  database can deadlock each other.
- **Manual `railway run alembic upgrade head`** — fine for a one-off, but it is not
  tied to a deploy, so a migration can be forgotten.

`AUTO_CREATE_TABLES` must stay `false` in production (the production guard rejects
it); migrations are the only supported way the schema changes.

### 1.3 Database must be PostGIS

The schema uses PostGIS (`location_geom`, `centroid`, `bbox`, `simplified_geom`)
and `pg_trgm` for place search. Migration `0002_enable_postgis` runs
`CREATE EXTENSION IF NOT EXISTS postgis`, which needs the extension available on
the server. Railway's stock Postgres plugin does not enable PostGIS, so either:

- use a PostGIS-enabled Postgres (a Railway template or community plugin), or
- connect once as the database owner and run `CREATE EXTENSION postgis;`
  (and `CREATE EXTENSION pg_trgm;`) manually before the first deploy.

If the extension is missing, the migration fails loudly at startup and the
container restarts — it never serves against a wrong schema.

### 1.4 Media storage is not persistent by default

`STORAGE_BACKEND=local` writes evidence images to `MEDIA_DIR` inside the
container, which is per-deploy on Railway: **uploads are lost on every deploy.**
Pick one:

- attach a Railway volume and point `MEDIA_DIR` at the mount, or
- set `STORAGE_BACKEND=s3` with `S3_BUCKET` / `S3_ACCESS_KEY` / `S3_SECRET_KEY`.

Evidence images are only ever served through authenticated routes, so this is a
durability problem, not a privacy one. The app logs a warning at boot when it sees
`APP_ENV=production` with local storage.

### 1.5 Jobs: Celery or inline

`JOBS_MODE=celery` is the default and what the production guard expects: a second
Railway service from the same repo, overriding the start command with

```
celery -A app.worker.celery_app worker --loglevel=info
```

For a single-service deployment, set `JOBS_MODE=inline` **and**
`ALLOW_INLINE_JOBS=true`. Without the second variable the app refuses to start,
because running image classification inside a request thread is how a small
deployment takes the API down.

### 1.6 Health checks

`railway.json` sets `healthcheckPath: "/health"`. That endpoint needs no
authentication and no database:

- `GET /health` → `{"status":"ok", ...}` — liveness, used by Railway.
- `GET /api/v1/health/live` → same idea under the versioned prefix.
- `GET /api/v1/health/ready` → checks the database, the Alembic revision and PostGIS;
  returns `503` when the schema is not at head. Use this to verify a deploy
  manually; it is not a good platform health check because it fails while a
  migration is still running.

### 1.7 Environment variables for Railway

Nothing here has a usable default in production — the app refuses to start rather
than silently running against `localhost`.

**Required**

| Variable | Description |
| --- | --- |
| `APP_ENV` | `production`. Enables the strict startup guard. |
| `DATABASE_URL` | PostGIS-enabled Postgres DSN. Railway provides it as `postgresql://…`; the app rewrites it to `postgresql+psycopg://` for both the app and Alembic, so the raw form is fine. |
| `CORS_ALLOWED_ORIGINS` | Comma-separated browser origins, e.g. `https://ecoguard.vercel.app`. **The sign-in request is rejected with 403 if the Vercel origin is missing here.** (`ALLOWED_ORIGINS` is accepted as an alias.) |
| `COOKIE_SECURE` | `true`. Required by the production guard. |
| `SESSION_SAMESITE` | `none`. Required by the production guard: the web app and the API are different sites, so a `Lax` cookie is not sent on the cross-site sign-in request. |
| `REDIS_URL` | Redis DSN from the Redis plugin. Used for the response cache, the realtime event log and the auth rate limiter. |
| `AUTO_CREATE_TABLES` | `false`. Required; migrations own the schema. |
| `DEMO_ENABLED` | `false`. Required. |
| `ENABLE_API_DOCS` | `true` to serve `/api/docs`, `false` to hide it. |
| `SESSION_COOKIE` | Session cookie name. Only change it to move off an existing cookie during a staged rollout. |
| `SESSION_HOURS` | Session lifetime in hours, e.g. `12`. |
| `CACHE_TTL_SECONDS` | Map/config cache TTL, e.g. `15`. |
| `MAX_UPLOAD_MB` | Largest accepted upload, e.g. `10`. |
| `RUN_MIGRATIONS_ON_START` | `true` to apply migrations on boot (see 1.2). |
| `MEDIA_DIR` | Evidence directory, e.g. `/app/var/media` — point at a volume mount. |
| `STORAGE_BACKEND` | `s3` (durable) or `local` (ephemeral; see 1.4). |
| `JOBS_MODE` | `celery` (with a worker service) or `inline` (single service). |
| `FORWARDED_ALLOW_IPS` | `*` to trust the platform proxy's `X-Forwarded-For`. |

**Recommended / situational**

| Variable | Description |
| --- | --- |
| `ALLOW_INLINE_JOBS` | `true` only to permit `JOBS_MODE=inline` in production. |
| `IMAGE_ASSISTANCE` | `auto` (default) or `disabled`. Set `disabled` if you built with `INSTALL_ML=false`. |
| `SPECIESNET_MODEL` | Model reference, default `kaggle:google/speciesnet/pyTorch/v4.0.3b/1`. |
| `SPECIESNET_LOCAL_DIR` | Path to an already-downloaded model directory. Set it to skip the Kaggle download entirely when the weights are baked in or on a mounted volume. |
| `SPECIESNET_CACHE_DIR` | Where the weights are cached, e.g. `/app/var/models`. Mount a volume here or every cold start re-downloads them. |
| `SPECIESNET_WARMUP` | `true` to preload the classifier in the background at boot. |
| `SPECIESNET_DEVICE` | `cpu` (the only tested device). |
| `CONFIDENCE_THRESHOLD` | Minimum confidence to surface a suggestion, e.g. `0.75`. |
| `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Required when `STORAGE_BACKEND=s3`. |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | Optional serverless-Redis alternative to `REDIS_URL`. |
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | Optional alternative cache endpoint. |
| `SMS_PROVIDER`, `SMS_WEBHOOK_URL`, `SMS_WEBHOOK_TOKEN` | `disabled` (default) or `webhook` for advisory delivery. |
| `OSM_ATTRIBUTION`, `OSM_SOURCE_NAME` | Credit lines surfaced by `/config` for OSM-sourced data. |
| `MODEL_VERSION` | Optional build stamp reported alongside predictions. |

Not needed: all `VITE_*` variables. The MapTiler key is a **frontend** concern and
is compiled into the Vercel bundle at build time — never send it to the API.

---

## 2. Vercel (web app)

See the frontend's own documentation. The API-side requirements for a working
pairing:

- `CORS_ALLOWED_ORIGINS` must contain the exact Vercel origin (no trailing slash,
  no `*` — cookies require a specific origin).
- The session cookie must be `SameSite=None; Secure`, hence the two cookie
  variables above.
- The API must be reachable over HTTPS from the browser, which Railway provides.
- The `Origin` header is checked on every non-GET request as well, so a missing
  CORS entry fails sign-in with a plain `403 {"detail":"This origin is not
  allowed."}` — not a CORS error.
