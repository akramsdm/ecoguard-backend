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

`docker-compose.yml` runs the whole backend — Postgres (with the **PostGIS**
extension), Redis and the API — so no local Python or database install is
needed. Postgres is reachable on `localhost:5432` as `postgres` / `postgres`
(database `ecoguard`), which is what `.env` already expects, so the native venv
workflow above keeps working too.

```bash
docker compose up -d --build        # db + redis + api on :8000
python -m alembic upgrade head      # fresh DB: build the schema from migrations
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

Database schema changes are managed with Alembic. Fresh databases should be
initialized with:

```bash
python -m alembic upgrade head
```

For an existing development database that already has the current schema, first
stamp the baseline revision and then upgrade:

```bash
python -m alembic stamp 0001_baseline_schema
python -m alembic upgrade head
```

Use `python -m alembic revision --autogenerate -m 'message'` for future schema
changes. Note that the OSM geographic tables (`areas_osm`, `areas_osm_aliases`,
`places`) intentionally live **outside** `Base.metadata` — the SQLite test
harness cannot create PostGIS geometry DDL. They are managed exclusively by the
hand-written `0003_osm_areas` migration, so autogenerate will propose dropping
them; ignore those revisions. The same applies to `0004_spatial_authorization`
(`user_area_assignments`, `report_areas`, the report geometry/location columns
and their indexes), which is also hand-written for the same reason.

The communities that reports must reference are seeded once with:

```bash
docker compose run --rm -e DEMO_ENABLED=true api \
  python -m app.cli seed-demo --password '<12+ character password>'
```

### OSM geographic areas (read-only)

OSM-backed boundaries (districts, national parks, protected areas, nature/game/
wildlife/forest reserves) are imported into `areas_osm` and exposed read-only at
`GET /api/v1/areas-osm` (list: bbox, `area_type`, `q` text search, pagination)
and `GET /api/v1/areas-osm/{id}` (full geometry). Both responses carry the OSM
attribution string, which is also published in `GET /api/v1/config` under
`osm_attribution` for the frontend. List items now also carry the `active` gate
used by the frontend assignment picker; an administrator toggles that gate with
`PATCH /api/v1/admin/areas-osm/{id}` — audited and non-destructive: deactivating
an area stops its assignments and containment counts (nothing is deleted).

Importing is a manual, idempotent operation that never runs on API startup:

```bash
# Prereq: the venv has dev requirements (osmium is needed to read the extract)
pip install -r requirements-dev.txt

# Dry run first: reports classification + geometry stats without writing
python -m app.import_osm_areas --extract var/osm/uganda-latest.osm.pbf --dry-run

# Then apply (upserts by (osm_type, osm_id) — safe to re-run)
python -m app.import_osm_areas --extract var/osm/uganda-latest.osm.pbf
```

The tag -> `area_type` mapping lives in `app/osm_areas_config.json` (confirmed
against the Uganda extract), so the import scope is tunable without touching the
importer. Assembly uses a disk-backed libosmium node-location index by default
(`--location-index sparse_file_array`) so a whole country extract stays inside
modest RAM; `flex_mem` is only for tiny extracts. Notable empirical findings
baked into the config: Uganda districts are
`boundary=administrative` **`admin_level=4`** (level 6 in the extract is
Rwanda/DRC border data); national parks are mostly `boundary=protected_area` +
`protect_class=2` (IUCN II); and `protect_class=15` in Uganda means wetlands,
not game reserves. The `other` class is an explicit catch-all entry for
`protect_class` values outside the IUCN set.
`simplify_tolerance_degrees` defaults to `0.01` (≈1 km at Ugandan
latitudes), tuned for country/regional web rendering while keeping small
protected areas recognizable; the full-resolution `geom` is always stored.
Invalid source geometries are repaired with `ST_MakeValid`; anything that
remains invalid (or whose ring cannot even be assembled) is counted and listed
by name in the import summary rather than dropped silently.

The optional legacy-mapping proposal (Prompt 3 input) is produced read-only by:

```bash
python scripts/legacy_area_mapping.py --output var/legacy_area_mapping.csv
```

That prints the `reporter@`, `reviewer@`, `publisher@` and `admin@`
`ecoguard.example.org` accounts that share the password you supplied.

### Spatial authorization (area assignments + per-report containment)

Migration `0004_spatial_authorization` adds the PostGIS-backed access model.
All of it lives outside `Base.metadata` and is driven through raw SQL in
`app/spatial.py`, gated on the engine dialect being PostgreSQL — the SQLite
harness keeps its legacy exact-membership gating, so out-of-area staff there
still get 404s.

- **`user_area_assignments`** — append-only grants (`area_osm_id`, `assigned_by`,
  `revoked_at`). Revoking a grant writes `revoked_at`; rows are never deleted.
  The active grant is enforced by a partial unique index
  (`uq_user_area_assignment_active ... WHERE revoked_at IS NULL`).
- **`report_areas`** — the `areas_osm` polygons that a report's stored true
  location intersects (`ST_Intersects` on `location_geom`). Client input never
  feeds it; it is recomputed server-side on create/update/backfill.
- **Report location** — every report carries one:
  `share_location=true` stores the precise point (`location_geom`) and a
  ~1 km grid-snapped public point (`public_geom`, generalised, `gps`);
  coordinates without sharing become a grid-snapped point (`manual`); no
  coordinates at all falls back to the chosen area's centroid (`area_only`).
  `latitude`/`longitude` remain the *generalised display* coordinates; the
  precise point never leaves `location_geom`.

A user may act on a report when they hold an active assignment whose
`area_osm_id` is in the report's `report_areas` — overlap with **any one**
polygon suffices (e.g. a park *or* its containing district). Staff outside
every assigned area see **redacted** read views and get 403 on mutations;
they never see 404 for submitted+ content.

```bash
# one-time backfill of assignments + every existing report's location:
python scripts/migrate_assignments.py \
  --mapping var/osm/confirmed_legacy_mapping.json \
  --report var/spatial_migration_report.txt
```

Route behaviour (PostGIS active; owner = report author; "in-area" = active
assignment overlapping the report's areas):

| Route | Owner/reporter | In-area staff | Out-of-area staff | Non-owner on a draft |
|---|---|---|---|---|
| `GET /reports`, `/reports/{id}` | 200 full | 200 full | 200 **redacted** | 404 |
| `PATCH`/`PUT /reports/{id}` | 200 | — | 403 | 404 |
| `POST /reports/{id}/submit` | 200 | — | 403 | 404 |
| `POST .../review` · `/assign` · `/close` | 200 | 200 | 403 | 404 |
| `GET .../evidence` · `.../messages` | 200 | 200 | 403 | 404 |
| `POST .../messages` | 201 (owner/submitter/in-area) | 201 | 403 | 404 |
| `GET /map?view=staff` · `/dashboard` | own cases | full rows | redacted rows | n/a |
| `GET /advisories` (staff) | full | full | redacted | n/a |
| `POST .../advisories/publish` · `POST .../retract` | 200 | 200 | 403 | n/a |
| `GET /reports/export.csv` | own rows only | acting areas only | acting areas only | n/a |
| `PATCH /admin/users/{id}` `POST /admin/users` | dual-write `User.areas` **and** assignments (by OSM area id) | | | |
| `GET /admin/users` · `/auth/me` | `user_view` also returns live `assignments` (OSM id, name, type) so the picker and my-areas never read the legacy `User.areas` mirror | | | |
| `GET /areas-osm` (list) | public; items include the `active` assignment gate | | | |
| `PATCH /admin/areas-osm/{id}` | admin-only active toggle (audited, non-destructive) | | | |
| `GET /admin/areas-osm/coverage` | admin-only per-area open load + staff count (uncached) | | | |
| `GET /my-areas` | staff-only own assignments with open-case counts | | | |

A redacted view omits `title`, `code`, `description`, `species`, contact
fields, evidence, messages, reviewer notes, assignee and precise coordinates —
out-of-area staff see only that a case exists at a generalised point.

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
.venv/Scripts/python.exe -m pytest -q
```

138 tests, no model weights, no network and no PyTorch required — the
classifier is stubbed. Real-model behaviour is covered separately by
`python scripts/speciesnet_smoke.py <image>`, which needs the ML extra.

`tests/test_spatial_authorization.py` requires a running PostgreSQL with
PostGIS: it creates throwaway scratch databases (the same pattern as
`test_osm_areas.py`) and asserts the full new contract, including the
out-of-area staff redacted-read/403 behaviours. The unit-feeding SQLite suite
keeps exercising the legacy gating.

A root `conftest.py` excludes `smoke_test.py` from collection — its
module-level code sets `APP_ENV=development`, which silently enables the live
auth rate limiter and makes full-suite runs fail with spurious 429s near the
end.

## Deployment (Vercel)

Project `ecoguard-api` → FastAPI entrypoint `app/main.py` is auto-detected.
Required environment variables (Vercel project):

- `DATABASE_URL` — Neon Postgres (`postgresql+psycopg://...` with Neon's pooler host, sslmode=require)
- `REDIS_URL` — Upstash Redis (`rediss://...`), used for cache + realtime event log
- `ALLOWED_ORIGINS` — frontend origin e.g. `https://ecoguard.vercel.app`
- `COOKIE_SECURE` = `true`, `SESSION_SAMESITE` = `none` (cross-site session cookie)
- `APP_ENV` = `development` (use Alembic to manage schema on cold start)
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
  database, Alembic revision state and PostGIS availability.
- `GET /api/v1/config` reports the live `image_assistance` state
  (`not_configured` / `degraded` / `ready`) and `ai_model_version`. Only the
  deployment's static fields are cached, so a model that finishes loading is
  never masked by a stale `degraded`.
- `degraded` is intentionally broad: it covers *still loading*, *weights or
  runtime absent* and *load failed*. `GET /api/v1/admin/image-assistance`
  (admin only) returns the specific reason.
- Development startup no longer creates tables automatically; run Alembic
  migrations first, then seed data if needed.
- Application logging is configured by `app/logging_config.py`, which attaches a
  handler to the `app` logger tree only. The root logger is left alone
  deliberately, so third-party libraries do not flood the log. The worker calls
  the same setup in `app/worker.py`.
- Every prediction writes `prediction.requested` and then either
  `prediction.completed` or `prediction.failed` to the `audit` table. The audit
  row references the `Prediction`, which holds the result; intermediate retries
  are not audited.