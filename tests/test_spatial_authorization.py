"""Spatial authorization (step 3): assignments, mandatory location, redacted reads.

These tests require a real PostGIS database — the scratch-database pattern from
test_osm_areas.py — because the SQLite harness cannot run geometry DDL and the
new authorization paths are PostGIS-only. The deliberately rewritten out-of-area
staff tests that used to pin 404 live here, asserting the new contract: read
routes return a redacted 200, mutating routes return 403, and the redacted
payload excludes evidence/messages/reporter/assignee/precise fields.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import spatial

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / 'alembic.ini'
FIXTURES = REPO_ROOT / 'tests' / 'fixtures' / 'osm'
MAINT_URL = 'postgresql+psycopg://postgres:postgres@localhost:5432/postgres'
PASSWORD = 'PassPhrase1234!'

# Fixture geometry (same box as uganda_simple.osm): the park sits inside the
# district, so a point in the park overlaps two OSM areas.
PARK_LON, PARK_LAT = 31.1, 0.15          # inside park AND district
DISTRICT_LON, DISTRICT_LAT = 31.4, 0.4    # district only
NOWHERE_LON, NOWHERE_LAT = 33.0, 1.0      # outside every fixture area


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
    name = 'ecoguard_spatial_' + uuid.uuid4().hex[:8]
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
    if not _postgres_available():
        pytest.skip('Local Postgres is unavailable for spatial authorization tests')
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


def _clean(engine) -> None:
    """Reset app-owned rows while keeping areas_osm (imported once) intact."""
    with engine.begin() as conn:
        conn.execute(text(
            'TRUNCATE user_area_assignments, report_areas, evidence, messages, '
            'reviews, case_events, advisories, sessions, audit, outbox, reports, '
            'users, areas RESTART IDENTITY CASCADE'))


def _osm_ids(engine) -> dict:
    from app import import_osm_areas
    with engine.connect() as conn:
        n = conn.execute(text('SELECT count(*) FROM areas_osm')).scalar()
    if not n:
        import_osm_areas.run_import(engine, str(FIXTURES / 'uganda_simple.osm'))
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name FROM areas_osm WHERE name LIKE 'Fixture %'")).all()
    by_name = {r[1]: r[0] for r in rows}
    assert {'Fixture District', 'Fixture National Park'} <= set(by_name), by_name
    return {'district': by_name['Fixture District'], 'park': by_name['Fixture National Park']}


def _seed_legacy_areas(engine) -> None:
    from app.models import Area
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        if s.execute(text("SELECT 1 FROM areas WHERE id='community-a'")).first() is None:
            s.add_all([
                Area(id='community-a', name='Community A',
                     latitude=PARK_LAT, longitude=PARK_LON, radius_km=10),
                Area(id='community-b', name='Community B',
                     latitude=DISTRICT_LAT, longitude=DISTRICT_LON, radius_km=10),
                Area(id='wetland-a', name='Wetland zone A',
                     latitude=NOWHERE_LAT, longitude=NOWHERE_LON, radius_km=10),
            ])
            s.commit()
    finally:
        s.close()


class _Seeder:
    def __init__(self, engine, ids):
        self.engine = engine
        self.ids = ids

    def _session(self):
        return sessionmaker(bind=self.engine, expire_on_commit=False)()

    def ensure_admin(self) -> str:
        from app.models import User
        from app.security import hash_password
        s = self._session()
        try:
            row = s.execute(text(
                "SELECT id FROM users WHERE email='admin@example.org'")).first()
            if row is None:
                s.add(User(email='admin@example.org', name='Access Admin',
                           password_hash=hash_password(PASSWORD),
                           roles=['admin', 'reviewer', 'publisher', 'responder'],
                           areas=['community-a', 'community-b', 'wetland-a']))
                s.commit()
                row = s.execute(text(
                    "SELECT id FROM users WHERE email='admin@example.org'")).first()
            return row[0]
        finally:
            s.close()

    def add_user(self, email, name, roles, legacy=None, osm_ids=()) -> str:
        from app.models import User
        from app.security import hash_password
        s = self._session()
        try:
            admin_id = self.ensure_admin()
            u = User(email=email, name=name, password_hash=hash_password(PASSWORD),
                     roles=list(roles), areas=list(legacy or []))
            s.add(u)
            s.flush()
            for aid in osm_ids:
                spatial.grant_assignment(s, u.id, aid, admin_id)
            s.commit()
            return u.id
        finally:
            s.close()

    def grant(self, user_id, *osm_ids) -> None:
        s = self._session()
        try:
            admin_id = self.ensure_admin()
            for aid in osm_ids:
                spatial.grant_assignment(s, user_id, aid, admin_id)
            s.commit()
        finally:
            s.close()


@pytest.fixture()
def env(scratch_pg):
    _clean(scratch_pg['engine'])
    ids = _osm_ids(scratch_pg['engine'])
    _seed_legacy_areas(scratch_pg['engine'])
    seed = _Seeder(scratch_pg['engine'], ids)
    yield {'engine': scratch_pg['engine'], 'ids': ids, 'seed': seed}


@contextlib.contextmanager
def _client(env):
    from app import db as db_module
    from app.main import app
    from fastapi.testclient import TestClient

    original = db_module.SessionLocal
    db_module.SessionLocal = sessionmaker(bind=env['engine'], expire_on_commit=False)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        db_module.SessionLocal = original


def login(client, email):
    response = client.post('/api/v1/auth/login',
                           json={'email': email, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})
    return response.json()['user']


def new_report(client, area='community-a', lat=None, lon=None, share=False, **kw):
    payload = {'client_id': 'cl-' + uuid.uuid4().hex[:12], 'category': 'wildlife',
               'title': 'A new sighting in the area',
               'description': 'A detailed description of what was seen from a distance.',
               'area_id': area, 'observed_at': '2026-01-01T10:00:00Z',
               'consent': True, 'evidence_ids': [], 'share_location': share}
    if lat is not None:
        payload['latitude'] = lat
        payload['longitude'] = lon
    payload.update(kw)
    response = client.post('/api/v1/reports', json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def version_of(client, report_id):
    body = client.get(f'/api/v1/reports/{report_id}').json()
    assert 'version' in body, f'case {report_id} is not visible to this user: {body}'
    return body['version']


def submit(client, report_id, version=None):
    if version is None:
        version = version_of(client, report_id)
    response = client.post(f'/api/v1/reports/{report_id}/submit', json={'version': version})
    assert response.status_code == 200, response.text
    return response.json()


def review(client, report_id, version=None, decision='under_review', notes='Reviewed on the ground.'):
    if version is None:
        version = version_of(client, report_id)
    response = client.post(f'/api/v1/reports/{report_id}/review',
                           json={'version': version, 'decision': decision,
                                 'notes': notes, 'species': None})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Migration 0004
# --------------------------------------------------------------------------- #
def test_0004_schema_objects_and_clean_downgrade():
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
            assert {'user_area_assignments', 'report_areas'} <= tables
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='reports' AND column_name IN "
                "('location_geom','public_geom','location_precision','location_source')"))}
            assert cols == {'location_geom', 'public_geom', 'location_precision',
                            'location_source'}
            enums = {r[0] for r in conn.execute(text(
                "SELECT typname FROM pg_type WHERE typname IN "
                "('report_location_precision','report_location_source')"))}
            assert enums == {'report_location_precision', 'report_location_source'}
            for idx in ('ix_reports_location_geom', 'ix_reports_public_geom'):
                assert conn.execute(text(
                    "SELECT 1 FROM pg_indexes WHERE tablename='reports' AND indexname=:n"),
                    {'n': idx}).first() is not None
            assert conn.execute(text(
                "SELECT 1 FROM pg_indexes WHERE tablename='user_area_assignments' "
                "AND indexname='uq_user_area_assignment_active'")).first() is not None
        # Downgrade drops only the new objects; 0003 stays intact.
        command.downgrade(cfg, '0003_osm_areas')
        with engine.connect() as conn:
            tables = {r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
            assert not ({'user_area_assignments', 'report_areas'} & tables)
            assert {'areas_osm', 'areas'} <= tables
            assert conn.execute(text(
                "SELECT count(*) FROM pg_type WHERE typname IN "
                "('report_location_precision','report_location_source')")).scalar() == 0
    finally:
        engine.dispose()
        _drop_scratch(admin, name)
        admin.dispose()


# --------------------------------------------------------------------------- #
# Mandatory location on every report
# --------------------------------------------------------------------------- #
def test_shared_location_stores_precise_point_and_generalised_display(env):
    env['seed'].add_user('reporter@example.org', 'Reporter', ['reporter'])
    with _client(env) as client:
        login(client, 'reporter@example.org')
        rep = new_report(client, area='community-a', lat=PARK_LAT, lon=PARK_LON, share=True)
        s = sessionmaker(bind=env['engine'], expire_on_commit=False)()
        try:
            row = s.execute(text(
                'SELECT r.latitude, r.longitude, r.location_precision::text, '
                'r.location_source::text, ST_X(r.location_geom) AS glon, '
                'ST_Y(r.location_geom) AS glat, ST_X(r.public_geom) AS plon, '
                'ST_Y(r.public_geom) AS plat FROM reports r WHERE r.id = :i'),
                {'i': rep['id']}).mappings().first()
            areas = {r[0] for r in s.execute(text(
                'SELECT area_osm_id FROM report_areas WHERE report_id = :i'),
                {'i': rep['id']})}
        finally:
            s.close()
        assert row['location_precision'] == 'exact'
        assert row['location_source'] == 'gps'
        # True point is stored precisely and never displayed.
        assert (row['glon'], row['glat']) == (PARK_LON, PARK_LAT)
        # Public geometry and the legacy display columns are the generalised grid point.
        assert (row['plon'], row['plat']) == spatial.grid_point(PARK_LON, PARK_LAT)
        assert (row['longitude'], row['latitude']) == spatial.grid_point(PARK_LON, PARK_LAT)
        # The park sits inside the district: both polygons contain the point.
        assert areas == {env['ids']['park'], env['ids']['district']}


def test_report_without_location_falls_back_to_area_centroid(env):
    env['seed'].add_user('reporter@example.org', 'Reporter', ['reporter'])
    with _client(env) as client:
        login(client, 'reporter@example.org')
        rep = new_report(client, area='community-b')
        assert rep['latitude'] == DISTRICT_LAT and rep['longitude'] == DISTRICT_LON
        s = sessionmaker(bind=env['engine'], expire_on_commit=False)()
        try:
            row = s.execute(text(
                'SELECT location_precision::text AS p, location_source::text AS s '
                'FROM reports WHERE id = :i'), {'i': rep['id']}).mappings().first()
            areas = {r[0] for r in s.execute(text(
                'SELECT area_osm_id FROM report_areas WHERE report_id = :i'),
                {'i': rep['id']})}
        finally:
            s.close()
        assert row['p'] == 'generalised' and row['s'] == 'area_only'
        assert areas == {env['ids']['district']}


def test_grid_point_is_never_the_original_and_stays_within_a_cell():
    lon, lat = spatial.grid_point(31.12456789, 0.15673456)
    assert (lon, lat) != (31.12456789, 0.15673456)
    assert abs(lon - 31.12456789) <= spatial.GRID_DEGREES
    assert abs(lat - 0.15673456) <= spatial.GRID_DEGREES


def test_update_recomputes_report_areas_from_new_location(env):
    env['seed'].add_user('reporter@example.org', 'Reporter', ['reporter'])
    with _client(env) as client:
        login(client, 'reporter@example.org')
        rep = new_report(client, area='community-b')  # district-only first
        s = sessionmaker(bind=env['engine'], expire_on_commit=False)()
        try:
            assert {r[0] for r in s.execute(text(
                'SELECT area_osm_id FROM report_areas WHERE report_id = :i'),
                {'i': rep['id']})} == {env['ids']['district']}
        finally:
            s.close()
        r = client.put(f"/api/v1/reports/{rep['id']}", json={
            'client_id': rep['client_id'], 'category': 'wildlife',
            'title': 'A new sighting in the area',
            'description': 'A detailed description of what was seen from a distance.',
            'area_id': 'community-b', 'observed_at': '2026-01-01T10:00:00Z',
            'consent': True, 'evidence_ids': [], 'share_location': False,
            'latitude': PARK_LAT, 'longitude': PARK_LON,
            'version': version_of(client, rep['id'])})
        assert r.status_code == 200, r.text
        s = sessionmaker(bind=env['engine'], expire_on_commit=False)()
        try:
            areas = {r[0] for r in s.execute(text(
                'SELECT area_osm_id FROM report_areas WHERE report_id = :i'),
                {'i': rep['id']})}
        finally:
            s.close()
        assert areas == {env['ids']['park'], env['ids']['district']}


# --------------------------------------------------------------------------- #
# Authorization: overlap and redaction
# --------------------------------------------------------------------------- #
def _submitted_report_in_park(env, client):
    reporter_id = env['seed'].add_user('reporter@example.org', 'Reporter', ['reporter'])
    login(client, 'reporter@example.org')
    rep = new_report(client, area='community-a', lat=PARK_LAT, lon=PARK_LON, share=True)
    submit(client, rep['id'])
    return rep, reporter_id


def test_reviewer_assigned_to_park_can_act_on_case_inside_park(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer'], osm_ids=[env['ids']['park']])
    with _client(env) as client:
        rep, _ = _submitted_report_in_park(env, client)
        login(client, 'reviewer_park@example.org')
        viewed = client.get(f"/api/v1/reports/{rep['id']}")
        assert viewed.status_code == 200
        assert 'redacted' not in viewed.json()
        review(client, rep['id'], notes='Helped on the ground.')


def test_reviewer_assigned_to_containing_district_can_act_on_same_case(env):
    # Overlap rule: assignment to ANY containing area (park or district) suffices.
    env['seed'].add_user('reviewer_dist@example.org', 'District Reviewer',
                         ['reviewer'], osm_ids=[env['ids']['district']])
    with _client(env) as client:
        rep, _ = _submitted_report_in_park(env, client)
        login(client, 'reviewer_dist@example.org')
        viewed = client.get(f"/api/v1/reports/{rep['id']}")
        assert viewed.status_code == 200
        assert 'redacted' not in viewed.json()
        review(client, rep['id'], notes='A different reviewer, same outcome.')


def test_out_of_area_staff_reads_redacted_and_actions_are_blocked(env):
    """The rewritten 404-pinning contract: redacted 200 on read, 403 on actions."""
    env['seed'].add_user('reviewer_out@example.org', 'Outsider',
                         ['reviewer', 'responder'])
    with _client(env) as client:
        rep, _ = _submitted_report_in_park(env, client)
        login(client, 'reviewer_out@example.org')

        viewed = client.get(f"/api/v1/reports/{rep['id']}")
        assert viewed.status_code == 200
        body = viewed.json()
        assert body['redacted'] is True
        # present: the allowed facts
        assert body['category'] == 'wildlife'
        assert body['state'] == 'submitted'
        assert body['area_name'] == 'Community A'
        assert 'observed_at' in body
        # generalised location only — never the precise point
        assert (body['longitude'], body['latitude']) == spatial.grid_point(PARK_LON, PARK_LAT)
        # absent by field, not by accident: the sensitive payload is structurally empty
        for forbidden in ('code', 'title', 'description', 'species', 'consent',
                          'client_id', 'assignee_id', 'is_owner', 'evidence',
                          'timeline', 'reviews', 'version', 'share_location'):
            assert forbidden not in body, forbidden

        # read-only list includes the redacted row (200, not 404)
        listing = client.get('/api/v1/reports')
        assert listing.status_code == 200
        assert any(i.get('redacted') for i in listing.json()['items'])

        # dashboard still returns valid aggregates with redacted recent items
        dash = client.get('/api/v1/dashboard').json()
        assert dash['counts']['total'] >= 1
        assert any(i.get('redacted') for i in dash['recent'])

        # mutating routes are 403 (not 404) for out-of-area staff
        assert client.post(f"/api/v1/reports/{rep['id']}/review", json={
            'version': 2, 'decision': 'under_review', 'notes': 'Not mine to decide.',
            'species': None}).status_code == 403
        assert client.post(f"/api/v1/reports/{rep['id']}/messages",
                           json={'body': 'hello?'}).status_code == 403
        assert client.get(f"/api/v1/reports/{rep['id']}/messages").status_code == 403
        assert client.post(f"/api/v1/reports/{rep['id']}/assign", json={
            'version': 2, 'assignee_id': '00000000-0000-0000-0000-000000000000'}).status_code == 403
        assert client.post(f"/api/v1/reports/{rep['id']}/close", json={
            'version': 2, 'note': 'Cannot close what I cannot see.'}).status_code == 403


def test_staff_map_redacts_out_of_area_features(env):
    env['seed'].add_user('reviewer_out@example.org', 'Outsider', ['reviewer'])
    with _client(env) as client:
        rep, _ = _submitted_report_in_park(env, client)
        login(client, 'reviewer_out@example.org')
        data = client.get('/api/v1/map', params={'view': 'staff'}).json()
        feat = next(f for f in data['features'] if f['id'] == rep['id'])
        assert feat['properties']['redacted'] is True
        assert 'title' not in feat['properties']
        assert 'code' not in feat['properties']
        assert feat['properties']['precision'] == 'generalised'


def test_export_csv_excludes_out_of_area_rows(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer'], osm_ids=[env['ids']['park']])
    env['seed'].add_user('reporter2@example.org', 'Reporter Two', ['reporter'])
    with _client(env) as client:
        rep_park, _ = _submitted_report_in_park(env, client)
        login(client, 'reporter2@example.org')
        rep_district = new_report(client, area='community-b')
        submit(client, rep_district['id'])
        login(client, 'reviewer_park@example.org')
        exported = client.get('/api/v1/reports/export.csv')
        assert exported.status_code == 200
        csv_text = exported.text
        assert rep_park['code'] in csv_text        # park report: full, in-area
        assert rep_district['code'] not in csv_text  # district report: out-of-area


def test_plain_reporter_still_gets_404_on_someone_elses_report(env):
    # The redaction contract is scoped to authorized staff; a plain reporter is
    # not staff and must not learn that a report even exists.
    env['seed'].add_user('reporter1@example.org', 'Reporter One', ['reporter'])
    env['seed'].add_user('intruder@example.org', 'Intruder', ['reporter'])
    with _client(env) as client:
        rep, _ = _submitted_report_in_park(env, client)
        login(client, 'intruder@example.org')
        assert client.get(f"/api/v1/reports/{rep['id']}").status_code == 404
        assert client.post(f"/api/v1/reports/{rep['id']}/submit",
                           json={'version': 1}).status_code == 404


# --------------------------------------------------------------------------- #
# Admin PATCH: assignments (areas_osm ids), dual-write, revocation history
# --------------------------------------------------------------------------- #
def _admin_and_target(env):
    admin_id = env['seed'].ensure_admin()
    uid = env['seed'].add_user('target@example.org', 'Target', ['reporter'])
    return admin_id, uid


def test_admin_patch_rejects_unknown_and_inactive_areas(env):
    _, uid = _admin_and_target(env)
    with _client(env) as client:
        login(client, 'admin@example.org')
        payload = {'roles': ['reviewer'], 'areas': ['999999'], 'active': True}
        assert client.patch(f'/api/v1/admin/users/{uid}', json=payload).status_code == 422
        payload = {'roles': ['reviewer'], 'areas': ['not-an-int'], 'active': True}
        assert client.patch(f'/api/v1/admin/users/{uid}', json=payload).status_code == 422
        # inactive area_osm id is rejected too
        try:
            with env['engine'].begin() as conn:
                conn.execute(text('UPDATE areas_osm SET active = false WHERE id = :i'),
                             {'i': env['ids']['park']})
            payload = {'roles': ['reviewer'], 'areas': [str(env['ids']['park'])], 'active': True}
            assert client.patch(f'/api/v1/admin/users/{uid}', json=payload).status_code == 422
        finally:
            with env['engine'].begin() as conn:
                conn.execute(text('UPDATE areas_osm SET active = true WHERE id = :i'),
                             {'i': env['ids']['park']})


def test_admin_patch_writes_assignments_dual_writes_and_keeps_history(env):
    _, uid = _admin_and_target(env)
    s = sessionmaker(bind=env['engine'], expire_on_commit=False)()
    try:
        with _client(env) as client:
            login(client, 'admin@example.org')
            r = client.patch(f'/api/v1/admin/users/{uid}', json={
                'roles': ['reviewer'], 'areas': [str(env['ids']['park'])], 'active': True})
            assert r.status_code == 200, r.text
            active = [row[0] for row in s.execute(text(
                'SELECT area_osm_id FROM user_area_assignments '
                'WHERE user_id = :u AND revoked_at IS NULL'), {'u': uid})]
            assert active == [env['ids']['park']]
            legacy = s.execute(text('SELECT areas FROM users WHERE id = :u'),
                               {'u': uid}).scalar()
            assert legacy == [str(env['ids']['park'])]  # dual-write to the legacy field

            # Reassign to the district: the park row is revoked, not deleted.
            r = client.patch(f'/api/v1/admin/users/{uid}', json={
                'roles': ['reviewer'], 'areas': [str(env['ids']['district'])], 'active': True})
            assert r.status_code == 200, r.text
            all_rows = [tuple(row) for row in s.execute(text(
                'SELECT area_osm_id, revoked_at IS NOT NULL '
                'FROM user_area_assignments WHERE user_id = :u ORDER BY assigned_at'),
                {'u': uid})]
            assert all_rows == [(env['ids']['park'], True), (env['ids']['district'], False)]
    finally:
        s.close()


def test_assign_requires_assignee_area_overlap(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer', 'responder'], osm_ids=[env['ids']['park']])
    env['seed'].add_user('responder_dist@example.org', 'District Responder',
                         ['responder'], osm_ids=[env['ids']['district']])
    responder_none = env['seed'].add_user(
        'responder_none@example.org', 'Unowned Responder', ['responder'])
    with _client(env) as client:
        rep, _ = _submitted_report_in_park(env, client)
        login(client, 'reviewer_park@example.org')
        # Positive control: the park report's point lies inside the district too,
        # so a district-assigned responder may be assigned to it (overlap rule).
        assigned = client.post(f"/api/v1/reports/{rep['id']}/assign", json={
            'version': version_of(client, rep['id']),
            'assignee_id': env['seed'].add_user(
                'responder_dist2@example.org', 'District Responder Two',
                ['responder'], osm_ids=[env['ids']['district']])})
        assert assigned.status_code == 200, assigned.text
        # A responder with no overlapping assignment is refused.
        refused = client.post(f"/api/v1/reports/{rep['id']}/assign", json={
            'version': version_of(client, rep['id']),
            'assignee_id': responder_none})
        assert refused.status_code == 422, refused.text


# --------------------------------------------------------------------------- #
# Advisories: staff redaction and editorial 403
# --------------------------------------------------------------------------- #
def _verified_report_in_park(env, client):
    rep, reporter_id = _submitted_report_in_park(env, client)
    login(client, 'reviewer_park@example.org')
    review(client, rep['id'], decision='verified', notes='Verified on the ground.')
    return rep


def test_advisory_out_of_area_staff_sees_redacted_and_cannot_edit(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer', 'publisher'], osm_ids=[env['ids']['park']])
    env['seed'].add_user('publisher_out@example.org', 'Outsider Publisher', ['publisher'])
    with _client(env) as client:
        rep = _verified_report_in_park(env, client)
        login(client, 'reviewer_park@example.org')
        created = client.post('/api/v1/advisories', json={
            'report_id': rep['id'], 'title': 'Verified wetland advisory',
            'body': 'A human-verified advisory for the community.', 'source': 'Verified field report',
            'expires_at': (datetime.now(timezone.utc)
                           + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')})
        assert created.status_code == 201, created.text
        adv = created.json()
        published = client.post(f"/api/v1/advisories/{adv['id']}/publish", json={
            'version': adv['version'], 'privacy_checked': True, 'evidence_checked': True})
        assert published.status_code == 200, published.text

        login(client, 'publisher_out@example.org')
        viewed = client.get(f"/api/v1/advisories/{adv['id']}")
        assert viewed.status_code == 200
        body = viewed.json()
        assert body['redacted'] is True
        assert body['category'] == 'wildlife' and body['state'] == 'published'
        for forbidden in ('title', 'body', 'source', 'report_id', 'retraction_reason'):
            assert forbidden not in body, forbidden
        listing = client.get('/api/v1/advisories', params={'staff': 'true'})
        assert listing.status_code == 200
        assert any(i.get('redacted') for i in listing.json()['items'])
        assert client.post(f"/api/v1/advisories/{adv['id']}/publish", json={
            'version': 2, 'privacy_checked': True, 'evidence_checked': True}).status_code == 403
        assert client.post(f"/api/v1/advisories/{adv['id']}/retract", json={
            'version': 2, 'reason': 'Cannot retract what is not mine to see.'}).status_code == 403


# --------------------------------------------------------------------------- #
# Step-4 API surface: assignment exposure, area active toggle, coverage, my-areas
# --------------------------------------------------------------------------- #
def test_admin_users_and_me_expose_live_assignments(env):
    """user_view carries assignments so the picker works after any dual-write."""
    _, uid = _admin_and_target(env)
    with _client(env) as client:
        login(client, 'admin@example.org')
        r = client.patch(f'/api/v1/admin/users/{uid}', json={
            'roles': ['reviewer'], 'areas': [str(env['ids']['park'])], 'active': True})
        assert r.status_code == 200, r.text
        assert r.json()['assignments'] == [{
            'area_osm_id': env['ids']['park'], 'name': 'Fixture National Park',
            'area_type': 'national_park'}]
        # A fresh read of the directory reflects the write (no re-login needed).
        users = client.get('/api/v1/admin/users').json()['items']
        target = next(u for u in users if u['id'] == uid)
        assert {a['area_osm_id'] for a in target['assignments']} == {env['ids']['park']}
        # Reassign: the picker sees the change on the very next read.
        r = client.patch(f'/api/v1/admin/users/{uid}', json={
            'roles': ['reviewer'], 'areas': [str(env['ids']['district'])], 'active': True})
        assert r.status_code == 200, r.text
        users = client.get('/api/v1/admin/users').json()['items']
        target = next(u for u in users if u['id'] == uid)
        assert {a['area_osm_id'] for a in target['assignments']} == {env['ids']['district']}
        assert target['assignments'][0]['name'] == 'Fixture District'


def test_areas_osm_list_exposes_active_flag(env):
    with _client(env) as client:
        # The list is public (stable public facts only), like the single read.
        data = client.get('/api/v1/areas-osm', params={'limit': 100}).json()
        assert data['total'] >= 2
        assert all('active' in i for i in data['items'])
        park = next(i for i in data['items'] if i['id'] == env['ids']['park'])
        assert park['active'] is True
        with env['engine'].begin() as conn:
            conn.execute(text('UPDATE areas_osm SET active = false WHERE id = :i'),
                         {'i': env['ids']['park']})
        data = client.get('/api/v1/areas-osm', params={'limit': 100}).json()
        park = next(i for i in data['items'] if i['id'] == env['ids']['park'])
        assert park['active'] is False


def test_admin_toggles_osm_area_active(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer'], osm_ids=[env['ids']['park']])
    with _client(env) as client:
        login(client, 'admin@example.org')
        off = client.patch(f"/api/v1/admin/areas-osm/{env['ids']['park']}",
                           json={'active': False})
        assert off.status_code == 200 and off.json()['active'] is False
        # A deactivated area can no longer be assigned (valid_assignment_ids).
        _, uid = _admin_and_target(env)
        refused = client.patch(f'/api/v1/admin/users/{uid}', json={
            'roles': ['reviewer'], 'areas': [str(env['ids']['park'])], 'active': True})
        assert refused.status_code == 422
        on = client.patch(f"/api/v1/admin/areas-osm/{env['ids']['park']}",
                          json={'active': True})
        assert on.status_code == 200 and on.json()['active'] is True
        accepted = client.patch(f'/api/v1/admin/users/{uid}', json={
            'roles': ['reviewer'], 'areas': [str(env['ids']['park'])], 'active': True})
        assert accepted.status_code == 200, accepted.text
        assert client.patch('/api/v1/admin/areas-osm/99999999',
                            json={'active': True}).status_code == 404
        # Non-admins cannot toggle areas; a signed-out caller cannot either.
        env['seed'].add_user('reviewer_out@example.org', 'Outsider', ['reviewer'])
        login(client, 'reviewer_out@example.org')
        assert client.patch(f"/api/v1/admin/areas-osm/{env['ids']['park']}",
                            json={'active': False}).status_code == 403


def test_admin_area_coverage_reports_open_load_and_staffing(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer'], osm_ids=[env['ids']['park']])
    with _client(env) as client:
        _submitted_report_in_park(env, client)  # leaves the session as reporter
        login(client, 'admin@example.org')
        cover = client.get('/api/v1/admin/areas-osm/coverage')
        assert cover.status_code == 200, cover.text
        items = {i['id']: i for i in cover.json()['items']}
        park = items[env['ids']['park']]
        assert park['active'] is True
        assert park['total_cases'] >= 1 and park['open_cases'] >= 1
        assert park['assigned_staff'] >= 1 and park['needs_staff'] is False
        district = items.get(env['ids']['district'])
        assert district is not None
        # The district contains the park point too, so the same report counts there.
        assert district['open_cases'] >= 1
        # Coverage is admin-only.
        env['seed'].add_user('reviewer_out@example.org', 'Outsider', ['reviewer'])
        login(client, 'reviewer_out@example.org')
        assert client.get('/api/v1/admin/areas-osm/coverage').status_code == 403


def test_my_areas_returns_own_assignments_with_open_case_load(env):
    env['seed'].add_user('reviewer_park@example.org', 'Park Reviewer',
                         ['reviewer'], osm_ids=[env['ids']['park']])
    with _client(env) as client:
        _submitted_report_in_park(env, client)  # creates reporter@example.org
        login(client, 'reviewer_park@example.org')
        mine = client.get('/api/v1/my-areas')
        assert mine.status_code == 200, mine.text
        items = mine.json()['items']
        assert len(items) == 1
        assert items[0]['area_osm_id'] == env['ids']['park']
        assert items[0]['name'] == 'Fixture National Park'
        assert items[0]['open_cases'] >= 1 and items[0]['total_cases'] >= 1
        # A staff member with no assignments gets an empty list, not an error.
        env['seed'].add_user('reviewer_out@example.org', 'Outsider', ['reviewer'])
        login(client, 'reviewer_out@example.org')
        assert client.get('/api/v1/my-areas').json()['items'] == []
        # A plain reporter is not staff: 403.
        login(client, 'reporter@example.org')
        assert client.get('/api/v1/my-areas').status_code == 403