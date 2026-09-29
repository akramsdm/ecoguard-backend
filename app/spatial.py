"""PostGIS-backed spatial authorization for EcoGuard.

Everything here is additive and PostgreSQL/PostGIS-only: the tables and columns
are created by migration 0004 and live outside ``Base.metadata`` (the SQLite
test harness cannot create geometry DDL). Every public helper first checks
``postgis_active(db)``; when the engine is not PostgreSQL the callers keep their
legacy, exact-membership behaviour so the SQLite test suite still exercises the
area gating without duplicating geometry.

Authorization model (this step):
* A report's "acting areas" are the ``areas_osm`` polygons its stored true
  location (``location_geom``) intersects, precomputed in ``report_areas``.
* A user may act on a report if they hold an active (non-revoked)
  ``user_area_assignments`` row whose ``area_osm_id`` is one of the report's
  areas. Overlap with ANY one polygon (e.g. park *or* district) is enough.
* Staff outside every assigned area may still READ a redacted view.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Grid step for the generalised (public) location: ~1 km cells. Documented in
# README.md; picked so a precise point is never rendered as itself.
GRID_DEGREES = 0.00899


def postgis_active(db: Session) -> bool:
    dialect = getattr(getattr(db, 'bind', None), 'dialect', None)
    return dialect is not None and dialect.name == 'postgresql'


def grid_value(value: float) -> float:
    """Snap a coordinate to the ~1 km cell centre used for public output."""
    return round(value / GRID_DEGREES) * GRID_DEGREES


def grid_point(lon: float, lat: float) -> tuple[float, float]:
    return (grid_value(lon), grid_value(lat))


# --------------------------------------------------------------------------- #
# Assignments
# --------------------------------------------------------------------------- #

def active_area_ids(db: Session, user_id: str) -> frozenset[int]:
    """area_osm_id of every live assignment, restricted to active OSM areas."""
    if not postgis_active(db):
        return frozenset()
    rows = db.execute(text(
        'SELECT u.area_osm_id '
        'FROM user_area_assignments u '
        'JOIN areas_osm a ON a.id = u.area_osm_id '
        'WHERE u.user_id = :uid AND u.revoked_at IS NULL AND a.active'
    ), {'uid': user_id}).all()
    return frozenset(r[0] for r in rows)


def active_assignment_names(db: Session, user_id: str) -> list[dict]:
    """Live assignments with display names (lookups/audit)."""
    if not postgis_active(db):
        return []
    rows = db.execute(text(
        'SELECT u.area_osm_id, a.name, a.display_name, a.area_type '
        'FROM user_area_assignments u '
        'JOIN areas_osm a ON a.id = u.area_osm_id '
        'WHERE u.user_id = :uid AND u.revoked_at IS NULL AND a.active '
        'ORDER BY a.name'
    ), {'uid': user_id}).mappings().all()
    return [{'area_osm_id': r['area_osm_id'], 'name': r['name'] or r['display_name'],
             'area_type': r['area_type']} for r in rows]


def grant_assignment(db: Session, user_id: str, area_osm_id: int,
                     assigned_by: str) -> None:
    """Idempotent grant; protected by the partial unique index on live rows."""
    # Explicit casts: psycopg3 prepared statements otherwise deduce `:uid`/`:by`
    # as `text` here and `character varying` in the WHERE clause, which fails.
    db.execute(text(
        'INSERT INTO user_area_assignments (user_id, area_osm_id, assigned_by) '
        'SELECT CAST(:uid AS varchar), CAST(:aid AS bigint), CAST(:by AS varchar) '
        'WHERE NOT EXISTS ('
        '  SELECT 1 FROM user_area_assignments u '
        '  WHERE u.user_id = CAST(:uid AS varchar) '
        '  AND u.area_osm_id = CAST(:aid AS bigint) AND u.revoked_at IS NULL)'
    ), {'uid': user_id, 'aid': int(area_osm_id), 'by': assigned_by})


def set_assignments(db: Session, user_id: str, area_osm_ids: list[int],
                    assigned_by: str) -> None:
    """Make the live assignment set exactly ``area_osm_ids`` (history kept)."""
    for aid in area_osm_ids:
        grant_assignment(db, user_id, aid, assigned_by)
    if area_osm_ids:
        keep = ', '.join(f':k{i}' for i in range(len(area_osm_ids)))
        params = {f'k{i}': int(aid) for i, aid in enumerate(area_osm_ids)}
        db.execute(text(
            f'UPDATE user_area_assignments SET revoked_at = now() '
            f'WHERE user_id = :uid AND revoked_at IS NULL AND area_osm_id NOT IN ({keep})'
        ), {**params, 'uid': user_id})
    else:
        db.execute(text(
            'UPDATE user_area_assignments SET revoked_at = now() '
            'WHERE user_id = :uid AND revoked_at IS NULL'
        ), {'uid': user_id})


def valid_assignment_ids(db: Session, area_ids: list[int] | None) -> frozenset[int]:
    """Subset of ``area_ids`` that exist and are active in ``areas_osm``."""
    if not postgis_active(db) or not area_ids:
        return frozenset()
    rows = db.execute(text(
        'SELECT id FROM areas_osm WHERE id = ANY(:ids) AND active'
    ), {'ids': [int(a) for a in area_ids]}).all()
    return frozenset(r[0] for r in rows)


# --------------------------------------------------------------------------- #
# report_areas (containment cache)
# --------------------------------------------------------------------------- #

def report_area_rows(db: Session, report_id: str) -> list[dict]:
    """Containing OSM areas for a report (display)."""
    if not postgis_active(db):
        return []
    rows = db.execute(text(
        'SELECT ra.area_osm_id, a.name, a.display_name, a.area_type '
        'FROM report_areas ra '
        'JOIN areas_osm a ON a.id = ra.area_osm_id '
        'WHERE ra.report_id = :rid ORDER BY a.name'
    ), {'rid': report_id}).mappings().all()
    return [{'area_osm_id': r['area_osm_id'], 'name': r['name'] or r['display_name'],
             'area_type': r['area_type']} for r in rows]


def report_area_ids(db: Session, report_id: str) -> frozenset[int]:
    if not postgis_active(db):
        return frozenset()
    rows = db.execute(text(
        'SELECT area_osm_id FROM report_areas WHERE report_id = :rid'
    ), {'rid': report_id}).all()
    return frozenset(r[0] for r in rows)


def overlaps_report(db: Session, user_id: str, report_id: str) -> bool:
    """Whether the user's live assignments intersect the report's areas."""
    if not postgis_active(db):
        return False
    return db.execute(text(
        'SELECT 1 FROM report_areas ra '
        'WHERE ra.report_id = :rid '
        'AND EXISTS ('
        '  SELECT 1 FROM user_area_assignments u '
        '  JOIN areas_osm a ON a.id = u.area_osm_id '
        '  WHERE u.user_id = :uid AND u.revoked_at IS NULL AND a.active '
        '  AND u.area_osm_id = ra.area_osm_id)'
    ), {'rid': report_id, 'uid': user_id}).first() is not None


def rebuild_report_areas(db: Session, report_id: str) -> None:
    """Recompute ``report_areas`` from the report's stored ``location_geom``.

    Never reads a client-supplied area list: containment is derived from the
    server-stored true point against ``areas_osm``.
    """
    if not postgis_active(db):
        return
    db.execute(text('DELETE FROM report_areas WHERE report_id = :rid'),
               {'rid': report_id})
    db.execute(text(
        'INSERT INTO report_areas (report_id, area_osm_id) '
        'SELECT CAST(:rid AS varchar), a.id FROM areas_osm a '
        'JOIN reports r ON r.id = CAST(:rid AS varchar) '
        'WHERE a.active AND ST_Intersects(a.geom, r.location_geom)'
    ), {'rid': report_id})


# --------------------------------------------------------------------------- #
# Report location (Step 2: a report always ends up with SOME point)
# --------------------------------------------------------------------------- #

_REPORT_GEOM_SQL = text(
    'UPDATE reports SET location_geom = ST_SetSRID(ST_MakePoint(:glon, :glat), 4326),'
    ' public_geom = ST_SetSRID(ST_MakePoint(:plon, :plat), 4326),'
    ' location_precision = CAST(:prec AS report_location_precision),'
    ' location_source = CAST(:src AS report_location_source)'
    ' WHERE id = :rid')


def store_report_location(db: Session, report_id: str, precise_lon: float,
                          precise_lat: float, public_lon: float,
                          public_lat: float, precision: str, source: str) -> None:
    """Write the four location columns.

    ``precise_*`` is the true point (used only for containment), ``public_*`` the
    generalised point (used for every display/redaction path).
    """
    if not postgis_active(db):
        return
    db.execute(_REPORT_GEOM_SQL, {
        'glon': float(precise_lon), 'glat': float(precise_lat),
        'plon': float(public_lon), 'plat': float(public_lat),
        'prec': precision, 'src': source, 'rid': report_id})


def resolve_location(area, latitude: float | None, longitude: float | None,
                     share_location: bool) -> dict | None:
    """Compute a report's location fields from form input.

    Returns a dict of storage parameters for PostgreSQL, or ``None`` when the
    caller should skip (SQLite harness). ``area`` is the chosen legacy
    :class:`Area` (its lat/lon are treated as the fallback centroid).
    """
    if latitude is None or longitude is None:
        # Nothing chosen: fall back to the selected area's centroid.
        return {
            'precise_lon': float(area.longitude), 'precise_lat': float(area.latitude),
            'public_lon': float(area.longitude), 'public_lat': float(area.latitude),
            'precision': 'generalised', 'source': 'area_only',
        }
    if share_location:
        public_lon, public_lat = grid_point(longitude, latitude)
        return {
            'precise_lon': float(longitude), 'precise_lat': float(latitude),
            'public_lon': public_lon, 'public_lat': public_lat,
            'precision': 'exact', 'source': 'gps',
        }
    # A rough (not explicitly shared) position: generalise it to the grid so the
    # stored true point is never more precise than what is displayed.
    public_lon, public_lat = grid_point(longitude, latitude)
    return {
        'precise_lon': public_lon, 'precise_lat': public_lat,
        'public_lon': public_lon, 'public_lat': public_lat,
        'precision': 'generalised', 'source': 'manual',
    }


# --------------------------------------------------------------------------- #
# Visibility model
# --------------------------------------------------------------------------- #

def report_mode(db: Session, user, report) -> str | None:
    """'full' | 'redacted' | None (not visible at all)."""
    if report.owner_id == user.id:
        return 'full'
    if not _is_case_staff(user) or report.state == 'draft':
        return None
    if not postgis_active(db):
        # SQLite harness keeps exact legacy membership as its fallback path.
        return 'full' if report.area_id in (user.areas or []) else None
    return 'full' if overlaps_report(db, user.id, report.id) else 'redacted'


def visibility_map(db: Session, user, reports) -> dict[str, str]:
    """Batch classification of already-visible candidate reports.

    Candidates come from an owner/staff-aware query (see ``visible_query`` in
    reports.py). Returns report_id -> mode for every visible report; a report id
    is absent only when the user cannot see it at all.
    """
    mode_map: dict[str, str] = {}
    staff_ids: list[str] = []
    for r in reports:
        if r.owner_id == user.id:
            mode_map[r.id] = 'full'
        elif r.state != 'draft':
            staff_ids.append(r.id)
    if not staff_ids:
        return mode_map
    if not _is_case_staff(user):
        return mode_map
    if not postgis_active(db):
        # Legacy query already restricted candidates to in-area rows.
        return {**mode_map, **{rid: 'full' for rid in staff_ids}}
    rows = db.execute(text(
        'SELECT DISTINCT ra.report_id FROM report_areas ra '
        'WHERE ra.report_id = ANY(:ids) AND EXISTS ('
        '  SELECT 1 FROM user_area_assignments u '
        '  JOIN areas_osm a ON a.id = u.area_osm_id '
        '  WHERE u.user_id = :uid AND u.revoked_at IS NULL AND a.active '
        '  AND u.area_osm_id = ra.area_osm_id)'
    ), {'ids': staff_ids, 'uid': user.id}).all()
    full_ids = {r[0] for r in rows}
    for rid in staff_ids:
        mode_map[rid] = 'full' if rid in full_ids else 'redacted'
    return mode_map


def _is_case_staff(user) -> bool:
    # Imported lazily to avoid a cycle with security.py.
    from .security import is_case_staff
    return is_case_staff(user)


# --------------------------------------------------------------------------- #
# Redacted serialization
# --------------------------------------------------------------------------- #

def redacted_view(report, area_name: str | None):
    """The only facts an out-of-area staff member may see.

    Precisely: category, state, legacy area name, observed_at, and the
    generalised display location (the reports.latitude/longitude columns hold the
    public point for every report after this step's backfill). No evidence,
    messages, reporter identity, review notes, assignee, title, description or
    precise coordinates.
    """
    return {
        'id': report.id,
        'category': report.category,
        'state': report.state,
        'area_id': report.area_id,
        'area_name': area_name,
        'observed_at': report.observed_at,
        'latitude': report.latitude,
        'longitude': report.longitude,
        'location_precision': 'generalised',
        'redacted': True,
    }