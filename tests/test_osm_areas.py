"""OSM areas: migration 0003, importer behaviour, spatial correctness, endpoints.

Importer and endpoint tests need a local PostGIS (the fixture extracts are tiny —
the full country extract is never run in the suite). Uses the same scratch-database
pattern as test_postgis_migrations.py.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / 'alembic.ini'
FIXTURES = REPO_ROOT / 'tests' / 'fixtures' / 'osm'
MAINT_URL = 'postgresql+psycopg://postgres:postgres@localhost:5432/postgres'


def _admin_engine():
    return create_engine(MAINT_URL, isolation_level='AUTOCOMMIT')


def _scratch_url(name: str) -> str:
    return f'postgresql+psycopg://postgres:postgres@localhost:5432/{name}'


def _make_alembic_cfg(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option('script_location', str(REPO_ROOT / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', url)
    return cfg


def _postgres_available() -> bool:
    try:
        admin = _admin_engine()
        with admin.connect() as conn:
            conn.execute(text('SELECT 1'))
        return True
    except OperationalError:
        return False
    finally:
        admin.dispose()


def _create_scratch(admin) -> str:
    name = 'ecoguard_osm_' + uuid.uuid4().hex[:8]
    with admin.begin() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    return name


def _drop_scratch(admin, name: str) -> None:
    url = _scratch_url(name)
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            conn.execute(text(
                'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"))
    finally:
        eng.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


@pytest.fixture(scope='module')
def scratch_pg():
    """A scratch database migrated to head (0003_osm_areas)."""
    if not _postgres_available():
        pytest.skip('Local Postgres is unavailable for OSM area tests')
    admin = _admin_engine()
    name = _create_scratch(admin)
    engine = create_engine(_scratch_url(name))
    try:
        cfg = _make_alembic_cfg(_scratch_url(name))
        command.upgrade(cfg, 'head')
        yield {'name': name, 'url': _scratch_url(name), 'engine': engine}
    finally:
        engine.dispose()
        _drop_scratch(admin, name)
        admin.dispose()


# --------------------------------------------------------------------------- #
# Migration 0003
# --------------------------------------------------------------------------- #
def test_0003_adds_osm_objects_and_downgrades_cleanly():
    if not _postgres_available():
        pytest.skip('Local Postgres is unavailable for migration checks')
    admin = _admin_engine()
    name = _create_scratch(admin)
    url = _scratch_url(name)
    engine = create_engine(url)
    try:
        cfg = _make_alembic_cfg(url)
        command.upgrade(cfg, 'head')
        with engine.connect() as conn:
            tables = {r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
            assert {'areas_osm', 'areas_osm_aliases', 'places'} <= tables
            exts = {r[0] for r in conn.execute(text(
                "SELECT extname FROM pg_extension"))}
            assert {'postgis', 'pg_trgm'} <= exts
            # Geometry GiST indexes present on every geometry column.
            idxs = {r[0] for r in conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename='areas_osm'"))}
            for col in ('geom', 'centroid', 'bbox', 'simplified_geom'):
                assert f'ix_areas_osm_{col}' in idxs
                kind = conn.execute(text(
                    "SELECT am.amname FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_am am ON am.oid = c.relam "
                    "WHERE c.relname = :n"), {'n': f'ix_areas_osm_{col}'}).first()
                assert kind and 'gist' in kind
            assert 'ix_areas_osm_name_trgm' in idxs
        # Downgrade drops only the new OSM objects; 0001/0002 stay intact.
        command.downgrade(cfg, '0002_enable_postgis')
        with engine.connect() as conn:
            tables = {r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
            assert not ({'areas_osm', 'areas_osm_aliases', 'places'} & tables)
            assert {'areas', 'users', 'reports', 'evidence'} <= tables
            assert conn.execute(text(
                "SELECT count(*) FROM pg_extension WHERE extname='postgis'")).scalar() == 1
            assert conn.execute(text(
                "SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'")).scalar() == 0
    finally:
        engine.dispose()
        _drop_scratch(admin, name)
        admin.dispose()


# --------------------------------------------------------------------------- #
# Importer: idempotency, repair, spatial correctness
# --------------------------------------------------------------------------- #
def _import_simple(engine):
    from app import import_osm_areas
    return import_osm_areas.run_import(engine, str(FIXTURES / 'uganda_simple.osm'))


def test_import_is_idempotent(scratch_pg):
    engine = scratch_pg['engine']
    first = _import_simple(engine)
    assert first['inserted_or_updated'] == 2
    assert first['per_type']['district'] == 1
    assert first['per_type']['national_park'] == 1
    assert first['aliases'] >= 3  # name aliases incl. "Mountain Park"/"Old Park"
    second = _import_simple(engine)
    assert second['inserted_or_updated'] == 2  # upsert, not duplicate
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM areas_osm')).scalar() == 2
        assert conn.execute(text('SELECT count(*) FROM areas_osm_aliases')).scalar() == \
            first['aliases']


def test_invalid_geometry_is_repaired_by_pipeline(scratch_pg):
    # A deliberately broken (self-intersecting) ring, like the bowtie fixture,
    # must be repaired by ST_MakeValid before insert — the same SQL the importer
    # uses on every feature.
    from app import import_osm_areas
    bowtie_gj = ('{"type":"Polygon","coordinates":[[[31.0,0.0],[31.5,0.0],'
                 '[31.0,0.5],[31.5,0.5],[31.0,0.0]]]}')
    with scratch_pg['engine'].begin() as conn:
        prep = conn.execute(import_osm_areas._GEOM_PREP_SQL,
                            {'gj': bowtie_gj, 'min_area_sqm': 0.0}).mappings().first()
    assert prep['raw_valid'] is False
    assert prep['mp_valid'] is True
    assert prep['insertable'] is True
    assert prep['geom_hex']


def test_unassemblable_feature_is_skipped_not_crashed(scratch_pg):
    # libosmium refuses to assemble the bowtie ring, so the importer must count
    # the feature as skipped (reported by name) without aborting the run.
    from app import import_osm_areas
    stats = import_osm_areas.run_import(engine=scratch_pg['engine'],
                                        extract_path=str(FIXTURES / 'bowtie.osm'))
    assert stats['total_areas'] == 0
    assert stats['skipped_assembly'] == ['way21 "Bowtie Nature Reserve"']


def test_spatial_points_hit_the_right_areas(scratch_pg):
    _import_simple(scratch_pg['engine'])
    engine = scratch_pg['engine']
    with engine.connect() as conn:
        def contains(lon, lat):
            return {r[0] for r in conn.execute(text(
                'SELECT a.area_type FROM areas_osm a '
                'WHERE ST_Contains(a.geom, ST_SetSRID(ST_MakePoint(:lo, :la), 4326))'),
                {'lo': lon, 'la': lat})}
        # In the park (which sits inside the district): both match (overlap case).
        assert contains(31.1, 0.15) == {'district', 'national_park'}
        # In the district but outside the park.
        assert contains(31.4, 0.4) == {'district'}
        # Outside any fixture area.
        assert contains(33.0, 1.0) == set()


# --------------------------------------------------------------------------- #
# Read endpoints against scratch Postgres
# --------------------------------------------------------------------------- #
def _patched_client(scratch_pg):
    from app import db as db_module
    from app.main import app
    from fastapi.testclient import TestClient

    original = db_module.SessionLocal
    db_module.SessionLocal = sessionmaker(bind=scratch_pg['engine'], expire_on_commit=False)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        db_module.SessionLocal = original


def test_areas_osm_endpoints_filter_and_paginate(scratch_pg):
    _import_simple(scratch_pg['engine'])
    for client in _patched_client(scratch_pg):
        base = '/api/v1/areas-osm'

        listing = client.get(base)
        assert listing.status_code == 200
        body = listing.json()
        assert body['total'] == 2
        assert body['attribution'] == '© OpenStreetMap contributors, ODbL'
        assert body['source_version']
        assert all('geom' not in item for item in body['items'])  # no geometry by default

        # bbox around just the park also intersects the district around it, so
        # assert the filter behaves: full box -> both, park box -> both (overlap
        # is real), district-only box -> district, far box -> none.
        full = client.get(base, params={'bbox': '31.0,0.0,31.5,0.5'})
        assert full.json()['total'] == 2
        district_only = client.get(base, params={'bbox': '31.30,0.30,31.45,0.45'})
        assert district_only.json()['total'] == 1
        assert district_only.json()['items'][0]['area_type'] == 'district'
        empty = client.get(base, params={'bbox': '33.0,1.0,33.5,1.5'})
        assert empty.json()['total'] == 0

        # area_type filter.
        districts = client.get(base, params={'area_type': 'district'})
        assert districts.json()['total'] == 1
        assert districts.json()['items'][0]['name'] == 'Fixture District'

        # text search on name and on an alias (trigram path).
        by_name = client.get(base, params={'q': 'National Park'})
        assert by_name.json()['total'] == 1
        by_alias = client.get(base, params={'q': 'Old Park'})
        assert by_alias.json()['total'] == 1
        assert by_alias.json()['items'][0]['name'] == 'Fixture National Park'

        # pagination.
        page1 = client.get(base, params={'limit': 1, 'offset': 0})
        page2 = client.get(base, params={'limit': 1, 'offset': 1})
        assert len(page1.json()['items']) == 1 and len(page2.json()['items']) == 1
        assert page1.json()['items'][0]['id'] != page2.json()['items'][0]['id']

        # explicit simplified geometry.
        with_geom = client.get(base, params={'include_geometry': 'true', 'area_type': 'district'})
        item = with_geom.json()['items'][0]
        assert item['simplified_geom']['type'] in ('Polygon', 'MultiPolygon')

        # detail view returns full geometry plus aliases.
        detail = client.get(base + f"/{item['id']}")
        assert detail.status_code == 200
        dbody = detail.json()
        assert dbody['geom']['type'] in ('Polygon', 'MultiPolygon')
        assert dbody['display_name'] == 'Fixture District'
        assert {'alias', 'source'} <= set(dbody['aliases'][0])

        assert client.get(base + '/999999').status_code == 404
        assert client.get(base, params={'area_type': 'bogus'}).status_code == 422
        assert client.get(base, params={'bbox': '31,0,32'}).status_code == 422


def test_config_exposes_osm_attribution():
    from fastapi.testclient import TestClient
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        body = client.get('/api/v1/config').json()
        assert body['osm_attribution'] == '© OpenStreetMap contributors, ODbL'
        assert body['osm_source_name']