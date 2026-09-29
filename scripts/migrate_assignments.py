"""Backfill spatial authorization data from legacy areas, with a verification report.

Additive, idempotent, and read-only over the legacy model:

* ``user_area_assignments`` is populated for every existing user from their
  legacy ``User.areas`` keys via the human-confirmed mapping produced by
  ``scripts/legacy_area_mapping.py`` and reviewed before this script runs.
  Grants are INSERT-if-absent only — nothing already in the table is touched or
  revoked (revocation happens through PATCH /admin/users going forward).
* Every existing report gets its location columns backfilled using the Step-2
  rules (existing lat/lon if present, else the chosen legacy area's centroid)
  and its ``report_areas`` recomputed from the stored point.
* The legacy tables, ``User.areas`` values and ``Report.area_id`` are NOT
  modified — this step only supplements them.

The mapping file is JSON:  ``{"legacy_area_key": [osm_area_id, ...], ...}``

Usage:
    .venv\\Scripts\\python.exe scripts/migrate_assignments.py \
        --mapping var/osm/confirmed_legacy_mapping.json \
        --report var/spatial_migration_report.txt
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import spatial

DEFAULT_URL = 'postgresql+psycopg://postgres:postgres@localhost:5432/ecoguard'
DEFAULT_MAPPING = Path(__file__).resolve().parents[1] / 'var' / 'osm' / 'confirmed_legacy_mapping.json'
DEFAULT_REPORT = Path(__file__).resolve().parents[1] / 'var' / 'spatial_migration_report.txt'


def _rows(conn, sql: str, **params):
    return conn.execute(text(sql), params).mappings().all()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping', default=str(DEFAULT_MAPPING))
    parser.add_argument('--report', default=str(DEFAULT_REPORT))
    parser.add_argument('--db-url', default=os.getenv('DATABASE_URL', DEFAULT_URL))
    parser.add_argument('--assigned-by', default=None,
                        help='Email of the admin recorded as grantor (default: first active admin)')
    args = parser.parse_args()

    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f'ERROR: mapping file not found: {mapping_path}')
        return 2
    mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
    if not isinstance(mapping, dict):
        print('ERROR: mapping must be a JSON object of {legacy_key: [osm_id, ...]}')
        return 2

    engine = create_engine(args.db_url, pool_pre_ping=True)
    started = time.time()
    with engine.connect() as conn:  # type: ignore[attr-defined]
        dialect = conn.dialect.name
        if dialect != 'postgresql':
            print(f'ERROR: this script requires PostgreSQL/PostGIS, got dialect "{dialect}".')
            return 2

    report_lines: list[str] = []
    flags: list[str] = []

    def note(line: str) -> None:
        print(line)
        report_lines.append(line)

    with Session(engine) as db:
        # -- 0. grantor (admin recorded as assigned_by) -----------------------
        if args.assigned_by:
            grantor = db.execute(text(
                "SELECT id FROM users WHERE email = :e AND active"), {'e': args.assigned_by}).first()
        else:
            grantor = db.execute(text(
                "SELECT id FROM users WHERE roles::jsonb ? 'admin' AND active "
                "ORDER BY created_at LIMIT 1")).first()
        if not grantor:
            print('ERROR: no active administrator found to record as the grantor.')
            return 2
        grantor_id = grantor[0]

        # -- 1. user_area_assignments from User.areas via the mapping ----------
        area_names = {r['id']: (r['name'] or r['display_name'])
                      for r in _rows(db, 'SELECT id, name, display_name FROM areas_osm')}
        users = _rows(db, 'SELECT id, email, name, roles, areas FROM users ORDER BY email')
        inserted = 0
        note(f'\n== user_area_assignments (from legacy User.areas) ==')
        for u in users:
            legacy = u['areas'] or []
            mapped_ids: list[int] = []
            unmapped = [k for k in legacy if k not in mapping]
            for key in legacy:
                for aid in mapping.get(key, []):
                    if aid not in mapped_ids:
                        mapped_ids.append(int(aid))
            for aid in mapped_ids:
                if aid not in area_names:
                    flags.append(f'user {u["email"]}: mapping points at unknown area_osm_id {aid}')
                    continue
                db.execute(text(
                    'INSERT INTO user_area_assignments (user_id, area_osm_id, assigned_by) '
                    'SELECT CAST(:uid AS varchar), CAST(:aid AS bigint), CAST(:by AS varchar) '
                    'WHERE NOT EXISTS ('
                    '  SELECT 1 FROM user_area_assignments u '
                    '  WHERE u.user_id = CAST(:uid AS varchar) '
                    '  AND u.area_osm_id = CAST(:aid AS bigint) AND u.revoked_at IS NULL)'
                ), {'uid': u['id'], 'aid': aid, 'by': grantor_id})
                inserted += 1
            pretty = ', '.join(f'{aid} ({area_names.get(aid, "?")})' for aid in mapped_ids) or '—'
            if unmapped:
                flags.append(f'user {u["email"]}: legacy areas without a confirmed mapping: {unmapped}')
                pretty += f'   [UNMAPPED: {", ".join(unmapped)}]'
            note(f'  {u["email"]:<45} roles={set(u["roles"])} '
                 f'legacy={legacy or "[]"} -> assignments={pretty}')
        db.commit()

        # -- 2. backfill report location + report_areas -------------------------
        reports = _rows(db, 'SELECT r.id, r.code, r.area_id, r.state, r.share_location, '
                            'r.latitude, r.longitude '
                            'FROM reports r ORDER BY r.created_at')
        areas = {a['id']: a for a in _rows(db, 'SELECT id, name, latitude, longitude FROM areas')}
        note(f'\n== report location backfill + report_areas ==')
        rebuilt = 0
        for r in reports:
            area = areas.get(r['area_id'])
            if area is None:
                flags.append(f'report {r["code"]}: legacy area_id "{r["area_id"]}" has no Area row')
                note(f'  {r["code"]:<10} area={r["area_id"]}  [NO LEGACY AREA]')
                continue
            loc = spatial.resolve_location(area, r['latitude'], r['longitude'], r['share_location'])
            if loc is None:
                flags.append(f'report {r["code"]}: not on a PostGIS engine; skipped')
                continue
            # Display columns become the public (generalised) point.
            db.execute(text(
                'UPDATE reports SET latitude = :lat, longitude = :lon WHERE id = :rid'
            ), {'lat': loc['public_lat'], 'lon': loc['public_lon'], 'rid': r['id']})
            spatial.store_report_location(db, r['id'], loc['precise_lon'], loc['precise_lat'],
                                          loc['public_lon'], loc['public_lat'],
                                          loc['precision'], loc['source'])
            spatial.rebuild_report_areas(db, r['id'])
            rebuilt += 1
            hits = spatial.report_area_rows(db, r['id'])
            if not hits:
                flags.append(f'report {r["code"]}: no OSM area contains its location point '
                             f'({loc["public_lon"]}, {loc["public_lat"]})')
            note(f'  {r["code"]:<10} area={r["area_id"]:<12} '
                 f'loc=({round(loc["public_lon"],4)},{round(loc["public_lat"],4)}) '
                 f'{loc["precision"]}/{loc["source"]} -> '
                 f'{[h["name"] for h in hits] or "NO MATCH"}')
        db.commit()

    # -- 3. verification report ------------------------------------------------
    summary = f'''
spatial authorization backfill — {time.strftime('%Y-%m-%d %H:%M:%S')}
runtime seconds: {round(time.time() - started, 1)}
users processed: {len(users)}
assignment rows granted: {inserted}
reports backfilled: {rebuilt}
flags: {len(flags)}
'''
    note(summary)
    if flags:
        note('== items needing manual review ==')
        for f in flags:
            note('  - ' + f)

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(report_lines) + '\n', encoding='utf-8')
    print(f'\nVerification report written to {out}')
    return 0 if not flags else 1


if __name__ == '__main__':
    raise SystemExit(main())