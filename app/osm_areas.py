"""Read-only API surface for OSM-backed geographic areas.

Built as a fresh ``/areas-osm`` pair of endpoints rather than extending
``GET /areas`` with a source filter: the legacy ``areas`` table keeps its
single, unchanged meaning for reports/advisories, and treating OSM areas as
their own resource keeps every geometry decision tabular and cacheable.

Public-safe by design: only stable public facts (name, type, geometry,
attribution) are exposed — nothing user-linked. Queries run on the PostGIS
trigram/GiST indexes created by migration 0003.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .security import current_user, require_role, audit
from .schemas import AreaActive

router = APIRouter(tags=['areas-osm'])

AREA_TYPES = (
    'district', 'national_park', 'protected_area', 'nature_reserve',
    'game_reserve', 'wildlife_reserve', 'forest_reserve', 'other',
)


def _osm_attribution() -> str:
    return get_settings().osm_attribution


def _require_postgis(db: Session) -> None:
    dialect = getattr(getattr(db, 'bind', None), 'dialect', None)
    if dialect is None or dialect.name != 'postgresql':
        raise HTTPException(503, 'OSM area queries require a PostGIS database.')


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in value.split(',')]
    if len(parts) != 4:
        raise HTTPException(422, 'bbox must be four comma-separated numbers: minLon,minLat,maxLon,maxLat')
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(422, 'bbox values must be numbers') from None
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise HTTPException(422, 'bbox values out of range')
    if minx >= maxx or miny >= maxy:
        raise HTTPException(422, 'bbox min values must be smaller than max values')
    return minx, miny, maxx, maxy


def _select_columns(include_geometry: bool) -> list:
    cols = [
        'a.id', 'a.osm_type', 'a.osm_id', 'a.area_type', 'a.admin_level',
        'a.name', 'a.display_name', 'a.source_version', 'a.active',
        'ST_AsGeoJSON(a.centroid) AS centroid',
    ]
    if include_geometry:
        cols.append('ST_AsGeoJSON(a.simplified_geom) AS simplified_geom')
    return cols


@router.get('/areas-osm')
def list_areas_osm(
    db: Session = Depends(get_db),
    bbox: str | None = Query(None, description='minLon,minLat,maxLon,maxLat'),
    area_type: str | None = Query(None, description='Filter by the confirmed area_type vocabulary.'),
    q: str | None = Query(None, min_length=1, description='Name/alias text search (trigram index).'),
    include_geometry: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _require_postgis(db)
    where = []
    params: dict = {}
    if bbox:
        minx, miny, maxx, maxy = _parse_bbox(bbox)
        where.append('ST_Intersects(a.geom, ST_MakeEnvelope(:bbox_minx, :bbox_miny, :bbox_maxx, :bbox_maxy, 4326))')
        params.update({'bbox_minx': minx, 'bbox_miny': miny, 'bbox_maxx': maxx, 'bbox_maxy': maxy})
    if area_type is not None:
        if area_type not in AREA_TYPES:
            raise HTTPException(422, f'area_type must be one of: {", ".join(AREA_TYPES)}')
        where.append('a.area_type = :area_type')
        params['area_type'] = area_type
    if q:
        where.append(
            "(a.name ILIKE '%' || :q || '%' OR a.display_name ILIKE '%' || :q || '%' "
            'OR :q = ANY(a.alt_names) '
            'OR EXISTS (SELECT 1 FROM areas_osm_aliases al '
            "WHERE al.area_osm_id = a.id AND al.alias ILIKE '%' || :q || '%'))"
        )
        params['q'] = q

    sql_where = ('WHERE ' + ' AND '.join(where)) if where else ''
    select_cols = ', '.join(_select_columns(include_geometry))
    rows = db.execute(text(
        f'SELECT {select_cols} FROM areas_osm a {sql_where} '
        'ORDER BY a.id LIMIT :limit OFFSET :offset'
    ), {**params, 'limit': limit, 'offset': offset}).mappings().all()
    total = db.execute(text(f'SELECT count(*) FROM areas_osm a {sql_where}'), params).scalar()
    source_version = db.execute(text('SELECT max(source_version) FROM areas_osm')).scalar()

    items = []
    for row in rows:
        item = {
            'id': row['id'],
            'osm_type': row['osm_type'],
            'osm_id': row['osm_id'],
            'area_type': row['area_type'],
            'admin_level': row['admin_level'],
            'name': row['name'],
            'display_name': row['display_name'],
            'source_version': row['source_version'],
            'active': row['active'],
            'centroid': json.loads(row['centroid']),
        }
        if include_geometry:
            item['simplified_geom'] = json.loads(row['simplified_geom'])
        items.append(item)

    return {
        'items': items,
        'total': total,
        'limit': limit,
        'offset': offset,
        'attribution': _osm_attribution(),
        'source_version': source_version,
    }


@router.get('/areas-osm/{area_id}')
def get_area_osm(area_id: int, db: Session = Depends(get_db)):
    _require_postgis(db)
    row = db.execute(text(
        """
        SELECT a.id, a.osm_type, a.osm_id, a.area_type, a.admin_level,
               a.name, a.display_name, a.alt_names, a.source_version, a.active,
               a.created_at, a.updated_at,
               ST_AsGeoJSON(a.centroid) AS centroid,
               ST_AsGeoJSON(a.bbox) AS bbox,
               ST_AsGeoJSON(a.geom) AS geom,
               ST_AsGeoJSON(a.simplified_geom) AS simplified_geom,
               COALESCE(array_agg(al.alias ORDER BY al.source, al.alias)
                        FILTER (WHERE al.alias IS NOT NULL), ARRAY[]::text[]) AS aliases,
               COALESCE(array_agg(al.source ORDER BY al.source, al.alias)
                        FILTER (WHERE al.alias IS NOT NULL), ARRAY[]::text[]) AS alias_sources
        FROM areas_osm a
        LEFT JOIN areas_osm_aliases al ON al.area_osm_id = a.id
        WHERE a.id = :area_id
        GROUP BY a.id
        """
    ), {'area_id': area_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, 'OSM area not found')
    return {
        'id': row['id'],
        'osm_type': row['osm_type'],
        'osm_id': row['osm_id'],
        'area_type': row['area_type'],
        'admin_level': row['admin_level'],
        'name': row['name'],
        'display_name': row['display_name'],
        'alt_names': row['alt_names'],
        'aliases': [{'alias': a, 'source': s} for a, s in zip(row['aliases'], row['alias_sources'])],
        'source_version': row['source_version'],
        'active': row['active'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'centroid': json.loads(row['centroid']),
        'bbox': json.loads(row['bbox']),
        'geom': json.loads(row['geom']),
        'simplified_geom': json.loads(row['simplified_geom']),
        'attribution': _osm_attribution(),
    }


@router.patch('/admin/areas-osm/{area_id}')
def set_area_active(area_id: int, payload: AreaActive,
                    user=Depends(current_user), db: Session = Depends(get_db)):
    """Toggle whether an OSM area may be assigned and used for containment.

    Deactivating an area does not delete its assignments or report_areas rows;
    it only stops them counting: ``active_area_ids``/rebuild/assignment-validity
    all filter on ``active``. History and evidence stay in place.
    """
    require_role(user, 'admin')
    result = db.execute(text(
        'UPDATE areas_osm SET active = :v, updated_at = now() WHERE id = :id'
    ), {'v': payload.active, 'id': area_id})
    if result.rowcount == 0:
        raise HTTPException(404, 'OSM area not found')
    audit(db, user, 'area.osm_active_changed', str(area_id))
    db.commit()
    return {'id': area_id, 'active': payload.active}