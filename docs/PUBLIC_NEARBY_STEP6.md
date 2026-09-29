# Step 6 — Public "near me" map (anonymous, GPS/manual/place-search, privacy by design)

Branch: `feature/public-nearby` (off `feature/live-maps`) — backend HEAD `2b12828`,
frontend HEAD `bfc7e64`. Whole-suite state at the end: backend **161 passed**, frontend
**53 passed**, `tsc -b` clean, `npm run build` green.

---

## 1. Realtime decision — and where it is in the code

**Decision: the public surface polls every 20 seconds. No anonymous SSE.**

SSE (`/stream`) is an authenticated, session-scoped subscription opened once in
`AppShell` (`src/AppShell.tsx`) — it is created only when `user` is set and
closed on logout. Wiring it to anonymous visitors would leak internal report
ids, timestamps and activity timing to anyone who can read the network. So the
public maps use the poll path that already existed for the community map:

- `src/pages/Community.tsx:77` — community map:
  `useMapData({view:'community', ..., interval:20000, realtime:false})`.
- `src/lib/useNearby.ts` — the new "near me" page: `NEARBY_POLL_MS = 20000`,
  20 000 ms interval while an active location exists, skipping ticks while the
  document is hidden.

The poll is cheap by construction: `/public/nearby` is server-cached on a
~1 km query grid, radius and category (`app/public.py:180-191`), so five
volunteers polling from the same grid cell share one backend hit. Interval-owning
screens always fetch (never a stale client cache entry), per the same contract
documented for step 5 in the frontend README.

## 2. Per-step diffs (how the roll-out arrived here)

| Step | Change | Where |
| --- | --- | --- |
| 1–2 | One shared map component; every screen renders through `MapPanel` / `AreaPolygonMap`, same tile profile, attributions, failure handling | `src/components/MapPanel.tsx` |
| 3 | PostGIS model: reports carry only the ~1 km grid point (`public_geom`, `gps` + generalise), greylist migration, `spatial.grid_point`; OSM areas imported | `app/models.py`, `app/spatial.py`, `alembic/versions/0003_…` |
| 4 | Auth-gated SSE + `useRealtime`; per-viewport `/map` fetching (debounced, LRU client cache) | `src/AppShell.tsx`, `src/lib/useMapData.ts`, `app/main.py` |
| 5 | Live maps + viewport fetching shipped; documented realtime decision; `VITE_MAPTILER_KEY` build gate | frontend README, `src/lib/tiles.ts` |
| 6 | **This step.** Anonymous surface: `/public/nearby`, `/public/places`, `/public/preferred-location`; gazetteer import; public map with location pinning; privacy rules | `app/public.py` (new), `app/import_osm_areas.py --with-places`, `alembic/versions/0005_public_nearby.py`, `src/pages/Nearby.tsx`, `src/lib/nearby.ts`, `src/lib/useNearby.ts` |

Diff citations:

```
git -C ecoguard-backend diff feature/live-maps...feature/public-nearby --stat
git -C ecoguard-frontend diff feature/live-maps...feature/public-nearby --stat
```

Step-6 additions in `feature/public-nearby` alone: `app/public.py`,
`tests/test_public_nearby.py`, `0005_public_nearby` migration, importer
`--with-places` + `place_kinds`, `src/pages/Nearby.tsx`, `src/lib/nearby.ts`,
`src/lib/useNearby.ts`, `src/lib/useNearby.test.tsx`, MapPanel location
marker/pick/initial-bounds/center props, and `'nearby'` in `PUBLIC_PAGES`.

## 3. Visibility rule — with the code ref

**A "nearby case" is a report behind an advisory that is `published` and not
`expired`** — the identical gate `list_advisories` applies for its public list
(`app/advisories.py:64`):

```python
q = q.filter(Advisory.state=='published', Advisory.expires_at>now())
```

`/public/nearby` applies the same rule in SQL
(`app/public.py:130`, `_NEARBY_SQL_TEMPLATE`, join at line 147):

```sql
JOIN advisories a ON a.report_id = r.id
                 AND a.state = :published AND a.expires_at > :cutoff
```

Verification/legal review state is irrelevant to visibility: a verified report
whose advisory is still a draft/expired is invisible; a draft never appears. The
SQLite fallback path applies the same gate through the ORM. The public object id
is the **advisory** id (`_case_feature`, `app/public.py:100-114`) — never a
report id. The payload is exhaustively `CASE_FIELDS` (`category, state,
distance_km, area_name, observed_at, published_at, location_precision`) and
carries the generalised (grid-snapped) point only — server responses for
`/public/*` never include title/body/evidence/messages/reporter/notes/assignee/
report_id/exact coordinates. `tests/test_public_nearby.py`
(`test_nearby_payload_carries_no_sensitive_fields`,
`test_nearby_exact_fix_never_served`) enforce this.

## 4. Max-radius and rate-limit reasoning

**Radius.** Default `10 km`, hard cap `50 km` (`app/public.py:52-53`, enforced
by the `Query(..., ge=1, le=MAX_RADIUS_KM)` on `radius_km`, line 167). Reasoning:
beyond 50 km "near me" stops being meaningful, and the ST_DWithin pre-filter on
`public_geom` plus the exact geography re-check stay cheap at the cap; a wider
area is what the country-wide community `/map` is for. Queries are bounded to
200 result rows and the degree pre-filter runs at 1.25× the radius margin so the
exact-radius rebind is just a correctness guard, not a scan.

**Rate limits.** Every `/public/*` route throttles per source IP through the
existing `RateLimiter` (`app/security.py:99`, 429 at line 120) — same class the
auth routes use, `APP_ENV=test` skip in conftest:

| Route | Limit | Period | Why |
| --- | --- | --- | --- |
| `/public/nearby` | 60 | 60 s | A 20 s poll is 3 req/min per client; 60/min serves a school/office sharing one public NAT IP with headroom. |
| `/public/places` | 120 | 60 s | Search-as-you-type can produce several requests per second locally; debounced client-side. |
| `/public/preferred-location` | 30 | 60 s | A write endpoint; lower budget discourages grinding a client-keyed store. |

The server Redis cache (TTL `cache_ttl_seconds`, 15 s) absorbs most poll
traffic; `debug.source` reports `cache | postgis | sqlite` honestly
(`app/public.py:186-191`).

**Privacy rules (operative).** Submitted lat/lon are used only for the distance
calculation and **never stored** (docstring `app/public.py:171-177`). The only
persisted point is via explicit opt-in to `/public/preferred-location`, which
writes `spatial.grid_point(...)` — the ~1 km rounded grid value — keyed by an
anonymous client id generated in `localStorage` (`src/lib/nearby.ts
getClientId`); precise fixes are never persisted. The UI explains this inline
(`<details className="privacy-note">`, save/remove controls) and never
auto-requests GPS: the map opens at Uganda country bounds and location comes
only from the explicit **GPS button**, a map tap/marker drag, or a place-search
hit.

## 5. Screenshots / descriptions

Captured from the running stack on 2026-09-29 (screens in `docs/screens/`):

| File | What it shows |
| --- | --- |
| `screens/step6-nearby-empty.png` | `/community/nearby` as an anonymous visitor: Uganda country bounds + district overlay, **no permission prompt**, explicit "Use my location" / search / tap-the-map controls, "No location yet" state |
| `screens/step6-nearby-results.png` | A location set (here: the opt-in saved location auto-loads); 10 km radius, marker on the map, "Nearby advisories" list with distance and the 20 s refresh note |
| `screens/step6-saved.png` | The privacy `<details>` opened — exact position used only for the request and never stored; the saved chip shows the coarsened ~1 km point (`0.198°, 30.099°`) with Remove |
| `screens/step6-place-search.png` | Place search against the OSM gazetteer (`/public/places`): type-ahead results listed in the search card |
| `screens/step6-place-picked.png` | A chosen place hit becomes the active location: map re-centres (zoom 12), the list refetches within the selected radius |

Map tap/marker-drag placement exists but is exercised by the automated click only in
the hook/component unit tests (`useNearby.test.tsx` "applies a manual pick",
MapPanel `setPickMode`); the headless renderer used for screenshots does not
reliably deliver a synthetic tap through the Leaflet input pipeline, so the
interactive screenshots above use the saved-location and place-search flows
instead — see §7.

## 6. Test results

```
# backend — full suite (run 2026-09-29)
python -m pytest tests -q            → 161 passed, 7 warnings (5m36s)
python -m pytest tests/test_public_nearby.py -q → 12 passed

# frontend
npx vitest run                        → 53 passed (5 files)
npx tsc -b                           → clean
npm run build                        → vite build green (tsc -b && vite build)
```

Step-6 coverage: anonymous access (cookie-less client hits `/public/nearby`),
radius boundary **A ≈0.55 km inside / B ≈1.11 km outside** a 1 km radius,
generalisation-never-bypassed (exact fix never served; grid point only),
field-absence scan over `CASE_FIELDS`, advisory-gate exclusion incl. expired
(back-dated via SQL), category filter + 422 caps, rate-limit enforcement
(`NEARBY_LIMIT=3` → 429), places combine + wildcard neutralisation, preferred
location coarsen/upsert/clear, SQLite fallback haversine + same gate, GPS
denied / ok / unsupported, manual pick, debounced place search, opt-in save,
20 s polling under fake timers, query building.

## 7. Gaps / known limitations

- **Access log leaks exact query coords.** uvicorn's default access log prints
  the raw query string of `/public/nearby?lat=…&lon=…`. App-level logs are
  already masked to 2 decimals (`app/public.py:_log`) and the value is never
  stored, but the access log is a documented residual gap. Recommended fix: a
  logging middleware that strips `lat`/`lon` from the request line, or
  `--no-access-log` in front of `uvicorn` behind the current proxy.
- **Gazetteer is a snapshot.** `places` is populated from the local
  `uganda-latest.osm.pbf` extract (one-off `import_places`, idempotent upsert);
  it is not auto-refreshed from live OSM.
- **Anonymous click-through stops at the list.** Advisory *detail* routes are
  auth-gated (`current_user`), so anonymous visitors see the public field set
  inline in the nearby list; only signed-in users open the detail page.
- **District overlay uses `/areas-osm` (already public).** The near-me map adds
  district boundaries from the pre-existing anonymous `/areas-osm` surface
  (fetched once per page view, not on each poll) rather than a new endpoint.
- **Browser-side preciseness.** The exact GPS fix lives only in React state
  during the session; nothing client-side persists it, and the only server-side
  record is the coarsened opt-in point.