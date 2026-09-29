"""Propose matches between the legacy ``areas`` circles and imported OSM areas.

Proposal only — this script never inserts, updates or deletes anything. It reads
the legacy centroid model and the ``areas_osm`` polygons, then writes a CSV of
candidate matches (best 3 per legacy area within the window) for human review
ahead of the (future) legacy-area migration step.

Usage:
    .venv\\Scripts\\python.exe scripts/legacy_area_mapping.py [--output var/legacy_area_mapping.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

from sqlalchemy import create_engine, text

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / 'var' / 'legacy_area_mapping.csv'
DEFAULT_URL = 'postgresql+psycopg://postgres:postgres@localhost:5432/ecoguard'


def _confidence(contains: bool, dist_m: float | None, radius_m: float) -> float:
    """Coarse, transparent confidence for review — never treated as final."""
    if contains:
        return 1.0
    if dist_m is None:
        return 0.0
    if dist_m <= radius_m:
        return 0.9
    if dist_m <= radius_m * 5:
        return 0.75
    return 0.5  # near enough to be worth a human look


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    parser.add_argument('--db-url', default=os.getenv('DATABASE_URL', DEFAULT_URL))
    parser.add_argument('--window-km', type=float, default=50.0,
                        help='Search radius around each legacy centroid.')
    args = parser.parse_args()

    engine = create_engine(args.db_url, pool_pre_ping=True)
    window_m = args.window_km * 1000.0
    rows_written = 0

    with engine.connect() as conn:
        legacy = conn.execute(text(
            'SELECT id, name, latitude, longitude, radius_km FROM areas ORDER BY id'
        )).mappings().all()
        if not legacy:
            print('No legacy areas found; nothing to propose.')
            return 0

        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', newline='', encoding='utf-8') as fh:
            writer = csv.writer(fh)
            writer.writerow(['legacy_area_key', 'legacy_name', 'candidate_osm_area_id',
                             'candidate_name', 'match_confidence',
                             'candidate_area_type', 'distance_m'])
            for area in legacy:
                radius_m = (area['radius_km'] or 0.0) * 1000.0 or 5000.0
                params = {
                    'lon': area['longitude'], 'lat': area['latitude'],
                    'window_m': window_m,
                }
                candidates = conn.execute(text(
                    """
                    WITH pt AS (
                        SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS g
                    )
                    SELECT a.id, a.area_type, a.name,
                           ST_Contains(a.geom, pt.g) AS contains,
                           ST_Distance(a.geom::geography, pt.g::geography) AS dist_m
                    FROM areas_osm a, pt
                    WHERE a.active
                      AND ST_DWithin(a.geom::geography, pt.g::geography, :window_m)
                    ORDER BY dist_m
                    LIMIT 3
                    """
                ), params).mappings().all()
                if not candidates:
                    writer.writerow([area['id'], area['name'], '', '', '', '', ''])
                    continue
                for cand in candidates:
                    conf = _confidence(cand['contains'], cand['dist_m'], radius_m)
                    writer.writerow([
                        area['id'], area['name'], cand['id'], cand['name'],
                        f'{conf:.2f}', cand['area_type'],
                        f'{cand["dist_m"]:.0f}' if cand['dist_m'] is not None else '',
                    ])
                    rows_written += 1
                print(f'{area["id"]:<16} {area["name"]} -> '
                      f'{len(candidates)} candidate(s) (top: '
                      f'{candidates[0]["name"]}, {candidates[0]["id"]})')
    print(f'\nWrote {rows_written} candidate rows to {out_path}')
    print('Nothing was written to the database.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())