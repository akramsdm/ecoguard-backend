"""Import OSM geographic areas into ``areas_osm`` (read-only source: OSM).

Usage (never runs on API startup)::

    python -m app.import_osm_areas --extract var/osm/uganda-latest.osm.pbf

The extract is read with pyosmium (libosmium): polygon closures are assembled by
libosmium and serialized to GeoJSON, then every feature is validated/repaired in
PostGIS (ST_MakeValid), simplified for web rendering, and upserted by
(osm_type, osm_id) so repeated runs are safe. Tag -> area_type classification is
driven entirely by ``app/osm_areas_config.json`` (the Step 0-confirmed set), so
the import scope can be changed without touching this file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, date, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

_LOGGER = logging.getLogger('import_osm_areas')
DEFAULT_CONFIG = Path(__file__).with_name('osm_areas_config.json')


# --------------------------------------------------------------------------- #
# Selection (pure Python, no DB involved)
# --------------------------------------------------------------------------- #
def _match_selector(tags: dict, selector: dict) -> bool:
    for key, expected in selector.items():
        if key not in tags:
            return False
        if expected == '*':
            continue
        if isinstance(expected, list):
            if tags[key] not in expected:
                return False
        elif tags[key] != expected:
            return False
    return True


def classify(tags: dict, config: dict):
    """Return the area_type for a feature's tags, or None to skip it entirely."""
    name = (tags.get('name') or tags.get('official_name') or '').lower()
    for entry in config['area_types']:
        if entry.get('name_contains'):
            if not any(p.lower() in name for p in entry['name_contains']):
                continue
        for selector in entry.get('match_any', []):
            if _match_selector(tags, selector):
                return entry['type']
    return None


def _alias_values(tags: dict, name_keys: list) -> list:
    """Distinct alias strings drawn from the configured name-ish tags, with
    OSM's multi-value separator conventions split apart."""
    seen = []
    for key in name_keys:
        value = tags.get(key)
        if not value:
            continue
        for part in str(value).replace(';', ',').split(','):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return seen


# --------------------------------------------------------------------------- #
# Geometry handling (PostGIS does the heavy lifting)
# --------------------------------------------------------------------------- #
_GEOM_PREP_SQL = text("""
WITH src AS (
    SELECT ST_SetSRID(ST_GeomFromGeoJSON(:gj), 4326) AS raw
),
repaired AS (
    SELECT ST_MakeValid(raw) AS g FROM src
),
norm AS (
    SELECT CASE
        WHEN upper(ST_GeometryType(g)) IN ('ST_POLYGON', 'ST_MULTIPOLYGON')
            THEN ST_Multi(ST_Force2D(g))
        WHEN upper(ST_GeometryType(g)) = 'ST_GEOMETRYCOLLECTION'
            THEN ST_CollectionExtract(ST_Multi(ST_Force2D(g)), 3)
        ELSE NULL
    END AS mp,
    g
    FROM repaired
),
meta AS (
    SELECT
        ST_IsValid(src.raw) AS raw_valid,
        ST_IsValid(r.g) AS repaired_valid,
        n.mp IS NOT NULL AS usable,
        ST_IsValid(n.mp) AS mp_valid,
        ST_IsEmpty(n.mp) AS mp_empty,
        ST_Dimension(n.mp) AS mp_dim,
        (n.mp IS NOT NULL AND ST_IsValid(n.mp) AND NOT ST_IsEmpty(n.mp)
            AND ST_Dimension(n.mp) = 2
            AND (:min_area_sqm <= 0 OR ST_Area(n.mp::geography) >= :min_area_sqm)) AS insertable,
        n.mp
    FROM src, repaired r, norm n
)
SELECT raw_valid, repaired_valid, usable, mp_valid, mp_empty, mp_dim, insertable,
       encode(ST_AsBinary(mp), 'hex') AS geom_hex
FROM meta
""")

_UPSERT_SQL = text("""
INSERT INTO areas_osm
    (osm_type, osm_id, area_type, admin_level, name, display_name, alt_names,
     geom, centroid, bbox, simplified_geom, source_version, active)
VALUES
    (:osm_type, :osm_id, :area_type, :admin_level, :name, :display_name, :alt_names,
     ST_SetSRID(ST_GeomFromWKB(decode(:geom_hex, 'hex')), 4326),
     ST_PointOnSurface(ST_SetSRID(ST_GeomFromWKB(decode(:geom_hex, 'hex')), 4326)),
     ST_Envelope(ST_SetSRID(ST_GeomFromWKB(decode(:geom_hex, 'hex')), 4326)),
     ST_SimplifyPreserveTopology(ST_SetSRID(ST_GeomFromWKB(decode(:geom_hex, 'hex')), 4326), :tol),
     :source_version, true)
ON CONFLICT (osm_type, osm_id) DO UPDATE SET
    area_type = EXCLUDED.area_type,
    admin_level = EXCLUDED.admin_level,
    name = EXCLUDED.name,
    display_name = EXCLUDED.display_name,
    alt_names = EXCLUDED.alt_names,
    geom = EXCLUDED.geom,
    centroid = EXCLUDED.centroid,
    bbox = EXCLUDED.bbox,
    simplified_geom = EXCLUDED.simplified_geom,
    source_version = EXCLUDED.source_version,
    active = true,
    updated_at = now()
RETURNING id
""")

_DELETE_ALIASES_SQL = text('DELETE FROM areas_osm_aliases WHERE area_osm_id = :aid')
_INSERT_ALIASES_SQL = text(
    'INSERT INTO areas_osm_aliases (area_osm_id, alias, source) VALUES (:aid, :alias, :source)')


# --------------------------------------------------------------------------- #
# Import runner
# --------------------------------------------------------------------------- #
def run_import(engine, extract_path, config=None, *, tolerance=None, min_area_sqm=None,
               limit=None, area_type=None, dry_run=False, location_index='sparse_file_array'):
    """Import ``extract_path`` into the engine's ``areas_osm`` tables.

    Returns a statistics dict. Idempotent: rerunning against the same extract
    upserts the same (osm_type, osm_id) rows instead of duplicating them.

    ``location_index`` selects the libosmium node-location index used to assemble
    areas: ``sparse_file_array`` (default) is disk-backed and works for whole
    country extracts on modest RAM; ``flex_mem`` keeps everything in memory and
    is only suitable for tiny extracts.
    """
    if config is None:
        config = load_config(DEFAULT_CONFIG)
    started = time.time()
    stats = {
        'extract': str(extract_path),
        'source_version': None,
        'total_areas': 0,
        'matched': 0,
        'per_type': Counter(),
        'repaired': 0,
        'skipped_invalid': [],          # invalid even after ST_MakeValid
        'skipped_unclassified': 0,
        'skipped_non_polygon': 0,
        'skipped_geom_error': 0,
        'skipped_min_area': 0,
        'skipped_assembly': [],          # libosmium could not assemble geometry
        'inserted_or_updated': 0,
        'aliases': 0,
        'dry_run': bool(dry_run),
        'runtime_seconds': None,
        'table_rows': None,
        'table_size_bytes': None,
    }
    tol = tolerance if tolerance is not None else config.get('simplify_tolerance_degrees', 0.01)
    min_area = min_area_sqm if min_area_sqm is not None else config.get('min_area_sqm', 0)
    name_alias_keys = config.get('name_alias_keys', [])
    allowed_types = {entry['type'] for entry in config['area_types']} | {'other'}

    _osmium = _lazy_osmium()
    stats['source_version'] = _extract_version(extract_path)

    from osmium.geom import GeoJSONFactory  # noqa: PLC0415

    collector_types = config.get('osm_types', ['way', 'relation'])
    gjf = GeoJSONFactory()

    class _StopImport(Exception):
        """Raised internally to stop application once ``limit`` is reached."""

    with engine.begin() as conn:
        def _process(area):  # noqa: ANN001 - libosmium Area
            """Classify, validate and (unless dry-run) upsert one area."""

            # -- classify --------------------------------------------------
            osm_type = 'way' if area.from_way else 'relation'
            if osm_type not in collector_types:
                return
            tags = {k: v for k, v in area.tags}
            try:
                geojson = gjf.create_multipolygon(area)
            except Exception as exc:
                name = tags.get('name') or tags.get('official_name') or '?'
                _LOGGER.warning('Could not assemble geometry for %s%s "%s": %s',
                                osm_type, area.orig_id(), name, exc)
                stats['skipped_assembly'].append(f'{osm_type}{area.orig_id()} "{name}"')
                return
            atype = classify(tags, config)
            stats['total_areas'] += 1
            if atype is None:
                stats['skipped_unclassified'] += 1
                return
            if (area_type and atype != area_type) or atype not in allowed_types:
                return
            stats['matched'] += 1
            if limit and stats['matched'] > limit:
                raise _StopImport()

            # -- geometry validation (PostGIS) -----------------------------
            params = {
                'gj': geojson,
                'min_area_sqm': float(min_area),
            }
            try:
                prep = conn.execute(_GEOM_PREP_SQL, params).mappings().first()
            except Exception as exc:  # pragma: no cover - defensive
                stats['skipped_geom_error'] += 1
                _LOGGER.warning('Geometry prep failed for %s%s (%s): %s',
                                osm_type, area.orig_id(), tags.get('name', '?'), exc)
                return
            if prep is None or not prep['usable']:
                stats['skipped_non_polygon'] += 1
                _LOGGER.warning('Not a usable polygon after repair: %s%s "%s" (%s)',
                                osm_type, area.orig_id(),
                                tags.get('name', tags.get('official_name', '?')),
                                ','.join(f'{k}={v}' for k, v in tags.items()
                                         if k in ('boundary', 'leisure', 'protect_class',
                                                  'protection_title', 'admin_level')))
                return
            if not prep['mp_valid']:
                stats['skipped_invalid'].append(
                    f'{osm_type}{area.orig_id()} '
                    f'"{tags.get("name", tags.get("official_name", "?"))}"')
                return
            if prep['mp_empty'] or prep['mp_dim'] != 2:
                stats['skipped_non_polygon'] += 1
                _LOGGER.warning('Repaired geometry is not a 2D polygon: %s%s "%s"',
                                osm_type, area.orig_id(), tags.get('name', '?'))
                return
            if not prep['insertable']:
                stats['skipped_min_area'] += 1
                return
            if not prep['raw_valid']:
                stats['repaired'] += 1
            stats['per_type'][atype] += 1
            if dry_run:
                return

            # -- write -----------------------------------------------------
            display_name = tags.get('name') or tags.get('official_name') or None
            alt_vals = _alias_values(tags, name_alias_keys)
            try:
                area_id = conn.execute(_UPSERT_SQL, {
                    'osm_type': osm_type,
                    'osm_id': area.orig_id(),
                    'area_type': atype,
                    'admin_level': _admin_level_int(tags.get('admin_level')),
                    'name': tags.get('name'),
                    'display_name': display_name,
                    'alt_names': alt_vals,
                    'geom_hex': prep['geom_hex'],
                    'tol': float(tol),
                    'source_version': stats['source_version'],
                }).scalar()
            except Exception as exc:
                _LOGGER.warning('Upsert failed for %s%s "%s": %s',
                                osm_type, area.orig_id(), tags.get('name', '?'), exc)
                stats['skipped_geom_error'] += 1
                return
            stats['inserted_or_updated'] += 1

            conn.execute(_DELETE_ALIASES_SQL, {'aid': area_id})
            alias_sources = ([('name', tags['name'])] if tags.get('name') else []) + \
                [(key, tags[key]) for key in name_alias_keys if tags.get(key)]
            for source, value in alias_sources:
                for alias in _split_aliases(value):
                    conn.execute(_INSERT_ALIASES_SQL, {'aid': area_id, 'alias': alias, 'source': source})
                    stats['aliases'] += 1

        class _StreamingHandler(_osmium.SimpleHandler):

            def area(self, area):
                _process(area)

        handler = _StreamingHandler()
        try:
            handler.apply_file(str(extract_path), locations=True, idx=location_index)
        except _StopImport:
            pass

    if not dry_run:
        with engine.connect() as conn:
            stats['table_rows'] = conn.execute(text('SELECT count(*) FROM areas_osm')).scalar()
            stats['table_size_bytes'] = conn.execute(
                text("SELECT pg_total_relation_size('areas_osm')")).scalar()
    stats['runtime_seconds'] = round(time.time() - started, 3)
    return stats


def _split_aliases(value: str) -> list:
    out = []
    for part in str(value).replace(';', ',').split(','):
        part = part.strip()
        if part:
            out.append(part)
    return out


def _admin_level_int(value) -> int | None:
    """Parse an OSM ``admin_level`` string defensively (e.g. ``'4'`` -> 4).

    OSM occasionally carries non-numeric values; those become NULL rather than
    aborting the feature's upsert.
    """
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lazy_osmium():
    try:
        import osmium  # noqa: PLC0415 - optional CLI dependency, not imported by the API
    except ImportError as exc:  # pragma: no cover - environment error path
        raise SystemExit(
            'osmium (pyosmium) is required to read the extract. Install dev requirements: '
            'pip install -r requirements-dev.txt') from exc
    return osmium


def _extract_version(extract_path) -> str:
    """Best-known date of the extract: the PBF header's replication timestamp
    (Geofabrik sets it), else today's date as a fallback."""
    osmium = _lazy_osmium()
    try:
        reader = osmium.io.Reader(str(extract_path))
        try:
            stamp = reader.header().get('osmosis_replication_timestamp')
        finally:
            reader.close()
        if stamp:
            return str(stamp)[:10]
    except Exception as exc:  # pragma: no cover - defensive
        _LOGGER.warning('Could not read extract header timestamp: %s', exc)
    return date.today().isoformat()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path=None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Import OSM areas into areas_osm (idempotent).')
    parser.add_argument('--extract', required=True, help='Path to an OSM extract (.osm.pbf or .osm).')
    parser.add_argument('--config', default=None, help='Path to a JSON config (defaults to app/osm_areas_config.json).')
    parser.add_argument('--db-url', default=None, help='SQLAlchemy database URL (default: DATABASE_URL or local ecoguard dev DB).')
    parser.add_argument('--tolerance', type=float, default=None, help='Simplification tolerance in degrees (default: from config).')
    parser.add_argument('--min-area-sqm', type=float, default=None, help='Skip features smaller than this area (default: from config).')
    parser.add_argument('--limit', type=int, default=None, help='Stop after N imported features (dry-run/testing).')
    parser.add_argument('--area-type', default=None, help='Restrict import to one area_type (testing).')
    parser.add_argument('--dry-run', action='store_true', help='Classify + validate only; do not touch the database.')
    parser.add_argument('--location-index', default='sparse_file_array',
                        help='libosmium node-location index: sparse_file_array (default, disk-backed) '
                             'or flex_mem (all-RAM, tiny extracts only).')
    parser.add_argument('--tmp-dir', default=None,
                        help='Directory for the disk-backed location index (default: system TEMP). '
                             'Point this at a drive with ample free space for country extracts.')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    config = load_config(args.config)
    if args.tmp_dir:
        # Redirect pyosmium's temp index before osmium is (lazily) imported and
        # creates its file-backed node-location table.
        tmp_dir = Path(args.tmp_dir).resolve()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        os.environ['TMP'] = os.environ['TEMP'] = str(tmp_dir)
        tempfile.tempdir = str(tmp_dir)
    url = args.db_url or os.getenv('DATABASE_URL') or \
        'postgresql+psycopg://postgres:postgres@localhost:5432/ecoguard'
    engine = create_engine(url, pool_pre_ping=True)

    stats = run_import(
        engine, args.extract, config,
        tolerance=args.tolerance, min_area_sqm=args.min_area_sqm,
        limit=args.limit, area_type=args.area_type, dry_run=args.dry_run,
        location_index=args.location_index,
    )
    _print_summary(stats)
    if args.tmp_dir:
        # pyosmium's CRT tempnam files (pattern "t????.0") are not unlinked on
        # Windows; remove any left behind by this run.
        for leftover in Path(args.tmp_dir).glob('t????.0'):
            try:
                leftover.unlink()
            except OSError:
                pass
    return 0


def _print_summary(stats) -> None:
    print(f"\nOSM import summary ({'DRY RUN' if stats['dry_run'] else 'applied'})")
    print(f"  extract           : {stats['extract']}")
    print(f"  source_version    : {stats['source_version']}")
    print(f"  total areas       : {stats['total_areas']}")
    print('  per area_type     : ' + (', '.join(f'{k}={v}' for k, v in sorted(stats['per_type'].items())) or 'none'))
    print(f"  repaired          : {stats['repaired']} (invalid source geometry made valid)")
    print(f"  skipped           : {len(stats['skipped_invalid'])} invalid after repair, "
          f"{stats['skipped_unclassified']} unclassified, {stats['skipped_non_polygon']} non-polygon, "
          f"{stats['skipped_geom_error']} geometry errors, {stats['skipped_min_area']} below min area, "
          f"{len(stats['skipped_assembly'])} un-assemblable")
    if stats['skipped_invalid']:
        print('  invalid-after-repair names: ' + '; '.join(stats['skipped_invalid'][:20]))
    if stats['skipped_assembly']:
        print('  un-assemblable features  : ' + '; '.join(stats['skipped_assembly'][:20]))
    if not stats['dry_run']:
        print(f"  inserted/updated  : {stats['inserted_or_updated']}")
        print(f"  aliases written   : {stats['aliases']}")
        print(f"  table rows        : {stats['table_rows']}")
        print(f"  table size        : {stats['table_size_bytes']} bytes")
    print(f"  runtime           : {stats['runtime_seconds']} s")


if __name__ == '__main__':
    raise SystemExit(main())