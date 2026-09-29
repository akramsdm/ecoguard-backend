"""Public, unauthenticated read surface (step 6): nearby cases, place search
and the optional coarsened saved location.

Privacy rules are hard, not advisory:

* Cases come only from reports behind an *active published advisory* — the same
  gate the public advisory list applies (``advisories.list_advisories``:
  ``state == 'published'`` and ``expires_at > now()``). A verified report with no
  published advisory is invisible here.
* Radius filtering and distances run on ``reports.public_geom``, never
  ``reports.location_geom`` — even when the reporter shared an exact GPS fix.
  The only point ever returned is the stored generalised one
  (``reports.latitude/longitude``, backfilled from ``public_geom`` in step 3).
* Every route is anonymous and rate-limited per source IP with the shared
  :class:`app.security.RateLimiter` (Redis in production, process-local in dev;
  skipped under ``APP_ENV=test``).

Known logging gap (documented, not silently hidden): uvicorn's access log prints
the raw query string, so a deployed instance logs the exact ``lat``/``lon`` of
``/public/nearby`` unless the access log is configured to strip query strings.
The application-level log lines below only ever record a ~2-decimal (≈1 km)
truncation, matching the generalisation grid's granularity.
"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .cache import cache
from .config import get_settings
from .db import get_db
from .models import Advisory, Area, Report, now
from .security import limiter
from . import spatial

logger = logging.getLogger('app.public')

router = APIRouter(prefix='/public', tags=['Public map'])

# --------------------------------------------------------------------------- #
# Knobs (deliberate, documented in README and the step-6 report)
# --------------------------------------------------------------------------- #
# Radius cap for /public/nearby. 50 km bounds query cost and radius abuse while
# still covering a practical day-trip radius; beyond that "near me" stops being
# meaningful and the /map community view is the right tool. Queries are bounded
# to 200 rows and the PostGIS path pre-indexes with ST_DWithin on public_geom.
DEFAULT_RADIUS_KM = 10.0
MAX_RADIUS_KM = 50.0
KM_PER_DEGREE = 111.195  # mean km per latitude degree (great-circle refinement below)

# Per-source-IP fixed windows via the existing RateLimiter. Polling at 20 s from
# one client is 3 req/min, so 60/min comfortably serves a school/office sharing
# one public IP; search-as-you-type gets 120/min.
NEARBY_LIMIT, NEARBY_PERIOD = 60, 60
PLACES_LIMIT, PLACES_PERIOD = 120, 60
PREFERRED_LIMIT, PREFERRED_PERIOD = 30, 60

# Exactly the keys a public case payload may carry (field-absence tests rely on
# this list being exhaustive).
CASE_FIELDS = ('category', 'state', 'distance_km', 'area_name',
               'observed_at', 'published_at', 'location_precision')


def _throttle(request: Request, limit: int, period: int, label: str) -> None:
    host = getattr(request.client, 'host', 'unknown')
    limiter.check(f'public-{label}:' + str(host), limit, period)


def _log(request: Request, endpoint: str, *, lat: float, lon: float,
         radius: float | None = None, category: str | None = None,
         count: int | None = None) -> None:
    """Masked application log line: coordinates are truncated to 2 decimals
    (~1.1 km), so the app's own logs never retain a precise client fix."""
    logger.info(
        '[public] %s ip=%s lat=%.2f lon=%.2f radius_km=%s category=%s hits=%s',
        endpoint, getattr(request.client, 'host', '?'),
        lat, lon, radius if radius is not None else '-',
        category or 'all', count if count is not None else '-')


def _nearby_body(features: list, debug: dict) -> dict:
    return {
        'type': 'FeatureCollection',
        'features': features,
        'clusters': [],
        'areas': [],
        'location_policy': 'Positions are generalised to a ~1 km grid; precise fixes are never exposed.',
        'attribution': get_settings().osm_attribution,
        'debug': debug,
    }


def _case_feature(advisory_id: str, category: str, state: str, observed_at: str,
                  published_at: str | None, area_name: str, lon: float | None,
                  lat: float | None, precision: str, distance_km: float) -> dict:
    return {
        'type': 'Feature',
        # The public object id is the advisory id (already public in the
        # advisories list / community map) — never the report id.
        'id': advisory_id,
        'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
        'properties': {
            'id': advisory_id, 'kind': 'case', 'category': category,
            'state': state, 'distance_km': round(float(distance_km), 1),
            'area_name': area_name, 'observed_at': observed_at,
            'published_at': published_at,
            'location_precision': precision,
        },
    }


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# --------------------------------------------------------------------------- #
# /public/nearby
# --------------------------------------------------------------------------- #
# Plain-string template: ``{category_clause}`` is substituted before text() is
# called. There are no other braces in the SQL body, so the substitution is safe.
_NEARBY_SQL_TEMPLATE = """
    SELECT report_id, advisory_id, category, state, observed_at, published_at,
           area_name, display_lon, display_lat, precision, distance_km
    FROM (
        SELECT DISTINCT ON (r.id)
               r.id AS report_id, a.id AS advisory_id, r.category, r.state,
               r.observed_at, a.published_at,
               ar.name AS area_name,
               COALESCE(r.longitude, ar.longitude) AS display_lon,
               COALESCE(r.latitude, ar.latitude) AS display_lat,
               CASE WHEN r.latitude IS NOT NULL THEN 'generalised'
                    ELSE 'community-centroid' END AS precision,
               ST_Distance(r.public_geom::geography,
                           ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography)
                   / 1000.0 AS distance_km
        FROM reports r
        JOIN advisories a ON a.report_id = r.id
                         AND a.state = :published AND a.expires_at > :cutoff
        JOIN areas ar ON ar.id = r.area_id
        WHERE r.public_geom IS NOT NULL
          AND ST_DWithin(r.public_geom,
                         ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :rad_deg)
        {category_clause}
        ORDER BY r.id, a.published_at DESC
    ) nearby
    WHERE distance_km <= :radius_km
    ORDER BY distance_km ASC, advisory_id
    LIMIT :limit
"""


@router.get('/nearby')
def nearby_cases(
    request: Request,
    db: Session = Depends(get_db),
    lat: float = Query(..., ge=-90, le=90, description='Query latitude (not stored).'),
    lon: float = Query(..., ge=-180, le=180, description='Query longitude (not stored).'),
    radius_km: float = Query(DEFAULT_RADIUS_KM, ge=1, le=MAX_RADIUS_KM),
    category: str | None = Query(None, pattern='^(wildlife|wetland|flood)$'),
    limit: int = Query(100, ge=1, le=200),
):
    """Reports behind an active published advisory within ``radius_km`` of the
    query point, ordered nearest-first.

    The submitted lat/lon is used only for the ST_DWithin calculation and is
    never written to the database; the response carries only the generalised
    public point. Requires no authentication.
    """
    _throttle(request, NEARBY_LIMIT, NEARBY_PERIOD, 'nearby')

    # Server cache on the ~1 km query grid, category and radius: nearby clients
    # share entries and a 20 s poll is cheap even at volume. Honest debug.source
    # (read the hit BEFORE the loader runs), mirroring /map's rework.
    glon, glat = spatial.grid_point(lon, lat)
    key = (f'public:nearby:{category or "all"}:r{radius_km}:'
           f'{glon:.4f},{glat:.4f}')
    hit = cache.get(key)
    if hit is not None:
        body = dict(hit.get('body') or {})
        debug = dict(hit.get('debug') or {})
        debug.update({'source': 'cache', 'cache_key': key})
        return {**body, 'debug': debug}

    features: list = []
    if spatial.postgis_active(db):
        params = {
            'lon': float(lon), 'lat': float(lat),
            'published': 'published', 'cutoff': now(),
            # Degree pre-filter with margin; the exact boundary is re-checked on
            # geography below so the radius is exact at the edge.
            'rad_deg': (float(radius_km) / KM_PER_DEGREE) * 1.25,
            'radius_km': float(radius_km), 'limit': int(limit),
        }
        category_clause = 'AND r.category = :category' if category else ''
        if category:
            params['category'] = category
        stmt = text(_NEARBY_SQL_TEMPLATE.replace('{category_clause}', category_clause))
        rows = db.execute(stmt, params).mappings().all()
        for row in rows:
            features.append(_case_feature(
                advisory_id=str(row['advisory_id']), category=row['category'],
                state=row['state'], observed_at=row['observed_at'],
                published_at=row['published_at'], area_name=row['area_name'],
                lon=float(row['display_lon']), lat=float(row['display_lat']),
                precision=row['precision'], distance_km=float(row['distance_km'])))
        source = 'postgis'
    else:
        # SQLite harness: same advisory gate, haversine distance in Python.
        query = (db.query(Report, Advisory, Area)
            .join(Advisory, Advisory.report_id == Report.id)
            .join(Area, Area.id == Report.area_id)
            .filter(Advisory.state == 'published',
                    Advisory.expires_at > now()))
        if category:
            query = query.filter(Report.category == category)
        rows = query.limit(2000).all()
        seen: set[str] = set()
        for report, advisory, area in rows:
            if report.id in seen:
                continue
            seen.add(report.id)
            dlon = report.longitude if report.longitude is not None else area.longitude
            dlat = report.latitude if report.latitude is not None else area.latitude
            if dlon is None or dlat is None:
                continue
            dist = _haversine_km(lon, lat, float(dlon), float(dlat))
            if dist > radius_km:
                continue
            precision = 'generalised' if report.latitude is not None else 'community-centroid'
            features.append(_case_feature(
                advisory_id=advisory.id, category=report.category, state=report.state,
                observed_at=report.observed_at, published_at=advisory.published_at,
                area_name=area.name, lon=float(dlon), lat=float(dlat),
                precision=precision, distance_km=dist))
        source = 'sqlite'

    features = features[:int(limit)]
    _log(request, 'nearby', lat=lat, lon=lon, radius=radius_km,
         category=category, count=len(features))
    body = _nearby_body(features, {'source': source, 'cache_key': key})
    cache.set(key, {'body': body, 'debug': body['debug']},
              get_settings().cache_ttl_seconds)
    return body


# --------------------------------------------------------------------------- #
# /public/places — gazetteer search (areas_osm + places, one representative point)
# --------------------------------------------------------------------------- #
_PLACES_SQL = text(
    """
    WITH hits AS (
        SELECT 'area'::text AS kind, a.id::text AS id,
               COALESCE(a.name, a.display_name) AS name, a.area_type AS area_type,
               ST_X(a.centroid) AS lon, ST_Y(a.centroid) AS lat,
               (a.name ILIKE :prefix OR a.display_name ILIKE :prefix) AS prefix_match
        FROM areas_osm a
        WHERE a.active
          AND (a.name ILIKE :like OR a.display_name ILIKE :like
               OR CAST(:q AS text) = ANY(a.alt_names)
               OR EXISTS (SELECT 1 FROM areas_osm_aliases al
                          WHERE al.area_osm_id = a.id AND al.alias ILIKE :like))
        UNION ALL
        SELECT 'place'::text, p.id::text, p.name, NULL,
               ST_X(p.geom), ST_Y(p.geom), (p.name ILIKE :prefix)
        FROM places p
        WHERE p.name ILIKE :like OR CAST(:q AS text) = ANY(p.alt_names)
    )
    SELECT kind, id, name, area_type, lon, lat
    FROM hits
    ORDER BY prefix_match DESC, (kind = 'area') DESC, name ASC
    LIMIT :limit
    """
)


@router.get('/places')
def search_places(
    request: Request,
    q: str = Query(..., min_length=2, max_length=80),
    limit: int = Query(8, ge=1, le=25),
    db: Session = Depends(get_db),
):
    """Combined place-name search over areas_osm and the places gazetteer.

    Returns one representative point per hit: the area centroid for an OSM
    area, the point itself for a gazetteer entry. Backed by the existing
    trigram indexes (areas_osm from 0003, places from 0005); no live external
    geocoding service is involved. Anonymous + rate-limited.
    """
    _throttle(request, PLACES_LIMIT, PLACES_PERIOD, 'places')
    if not spatial.postgis_active(db):
        raise HTTPException(503, 'Place search requires a PostGIS database.')
    # Strip wildcards so user input cannot broaden the trigram match.
    clean = q.replace('%', '').replace('_', '')
    if len(clean) < 2:
        return {'items': [], 'query': q, 'attribution': get_settings().osm_attribution}
    rows = db.execute(_PLACES_SQL, {
        'q': clean, 'like': f'%{clean}%', 'prefix': f'{clean}%', 'limit': int(limit),
    }).mappings().all()
    items = [{
        'id': row['id'], 'name': row['name'], 'kind': row['kind'],
        'area_type': row['area_type'], 'lat': float(row['lat']), 'lon': float(row['lon']),
    } for row in rows]
    _log(request, 'places', lat=0.0, lon=0.0, count=len(items))
    return {'items': items, 'query': q, 'attribution': get_settings().osm_attribution}


# --------------------------------------------------------------------------- #
# /public/preferred-location — opt-in, coarsened, client-keyed
# --------------------------------------------------------------------------- #
class PreferredLocationWrite(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str | None = Field(default=None, max_length=120)


@router.post('/preferred-location', status_code=201)
def save_preferred_location(payload: PreferredLocationWrite,
                            request: Request,
                            db: Session = Depends(get_db)):
    """Opt-in: store a coarsened version of the query location for future
    visits. The server snaps to the ~1 km public grid BEFORE writing, so a
    precise client fix is never persisted. Keyed by an anonymous client id."""
    _throttle(request, PREFERRED_LIMIT, PREFERRED_PERIOD, 'preferred-location')
    glon, glat = spatial.grid_point(payload.longitude, payload.latitude)
    db.execute(text(
        """
        INSERT INTO public_preferred_locations (client_id, latitude, longitude, name)
        VALUES (:cid, :lat, :lon, :name)
        ON CONFLICT (client_id) DO UPDATE SET
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            name = EXCLUDED.name,
            updated_at = now()
        """
    ), {'cid': payload.client_id, 'lat': glat, 'lon': glon, 'name': payload.name})
    db.commit()
    _log(request, 'preferred-save', lat=payload.latitude, lon=payload.longitude)
    return {
        'saved': True,
        'latitude': glat, 'longitude': glon,
        'precision': 'generalised',
        'note': 'Stored only as a ~1 km coarsened point, never the exact fix.',
    }


@router.get('/preferred-location')
def read_preferred_location(request: Request,
                            client_id: str = Query(min_length=8, max_length=64),
                            db: Session = Depends(get_db)):
    _throttle(request, PREFERRED_LIMIT, PREFERRED_PERIOD, 'preferred-location')
    row = db.execute(text(
        'SELECT latitude, longitude, name FROM public_preferred_locations '
        'WHERE client_id = :cid'), {'cid': client_id}).first()
    if row is None:
        raise HTTPException(404, 'No saved location for this device.')
    return {'latitude': row[0], 'longitude': row[1], 'name': row[2],
            'precision': 'generalised'}


@router.delete('/preferred-location', status_code=204)
def clear_preferred_location(request: Request,
                             client_id: str = Query(min_length=8, max_length=64),
                             db: Session = Depends(get_db)):
    _throttle(request, PREFERRED_LIMIT, PREFERRED_PERIOD, 'preferred-location')
    db.execute(text(
        'DELETE FROM public_preferred_locations WHERE client_id = :cid'),
        {'cid': client_id})
    db.commit()
    return None