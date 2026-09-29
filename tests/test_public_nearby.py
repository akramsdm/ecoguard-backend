"""Step 6: /public/nearby, /public/places, /public/preferred-location.

Anonymous surface. Requires the scratch-PostGIS pattern (ST_DWithin on
reports.public_geom for real radius maths); the SQLite harness fallback is
covered by a dedicated in-memory test at the bottom. Visibility mirrors the
public advisories gate (advisories.state == 'published' AND expires_at > now()).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from test_spatial_authorization import (
    _admin_engine, _scratch_url, _make_alembic_cfg, _postgres_available,
    _create_scratch, _drop_scratch, _clean, _osm_ids, _seed_legacy_areas,
    _Seeder, _client, login, new_report, submit, review,
    PARK_LON, PARK_LAT, DISTRICT_LON, DISTRICT_LAT, NOWHERE_LON, NOWHERE_LAT,
)

from app import spatial
from app.public import CASE_FIELDS

# Fixture park geometry (uganda_simple.osm) is a small box around (31.1, 0.15),
# so reports a few hundredths of a degree apart stay inside it and reviewable.
NEAR_A = (31.105, 0.15)   # ~0.55 km from the (31.110, 0.15) query point
NEAR_B = (31.120, 0.15)   # ~1.11 km from the same query point


@pytest.fixture(scope='module')
def scratch_pg():
    if not _postgres_available():
        pytest.skip('Local Postgres is unavailable for public-nearby tests')
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


@pytest.fixture()
def env(scratch_pg):
    _clean(scratch_pg['engine'])
    ids = _osm_ids(scratch_pg['engine'])
    _seed_legacy_areas(scratch_pg['engine'])
    seed = _Seeder(scratch_pg['engine'], ids)
    yield {'engine': scratch_pg['engine'], 'ids': ids, 'seed': seed}


def _staff(env):
    """Reporter + a reviewer/publisher assigned to the fixture park."""
    env['seed'].add_user('reporter_pub@example.org', 'Public Reporter', ['reporter'])
    env['seed'].add_user('pub_reviewer@example.org', 'Pub Reviewer',
                         ['reviewer', 'publisher'], osm_ids=[env['ids']['park']])


def _report_with_published_advisory(env, client, lon, lat, category='wildlife',
                                    area='community-a', expires_days=7):
    """A report with an exact GPS fix, verified and published as an advisory."""
    login(client, 'reporter_pub@example.org')
    rep = new_report(client, area=area, lat=lat, lon=lon, share=True, category=category)
    submit(client, rep['id'])
    login(client, 'pub_reviewer@example.org')
    review(client, rep['id'], decision='verified', notes='Verified on the ground.')
    created = client.post('/api/v1/advisories', json={
        'report_id': rep['id'], 'title': f'Verified {category} advisory',
        'body': 'A human-verified advisory for the community.', 'source': 'Verified field report',
        'expires_at': (datetime.now(timezone.utc)
                       + timedelta(days=expires_days)).strftime('%Y-%m-%dT%H:%M:%SZ')})
    assert created.status_code == 201, created.text
    adv = created.json()
    published = client.post(f"/api/v1/advisories/{adv['id']}/publish", json={
        'version': adv['version'], 'privacy_checked': True, 'evidence_checked': True})
    assert published.status_code == 200, published.text
    return rep, adv


def _nearby(client, lon=31.110, lat=0.15, radius_km=5, category=None):
    params = {'lat': lat, 'lon': lon, 'radius_km': radius_km}
    if category:
        params['category'] = category
    return client.get('/api/v1/public/nearby', params=params)


# --------------------------------------------------------------------------- #
# No auth, boundary correctness, visibility gate
# --------------------------------------------------------------------------- #
def test_nearby_requires_no_authentication(env):
    _staff(env)
    with _client(env) as client:
        # Build a published case while logged in...
        _report_with_published_advisory(env, client, *NEAR_A)
    with _client(env) as client:  # ...then query from a brand-new, cookie-less client
        assert client.cookies.get('ecoguard_session') is None
        assert 'Cookie' not in client.headers
        response = client.get('/api/v1/public/nearby',
                              params={'lat': 0.15, 'lon': 31.110, 'radius_km': 5})
        assert response.status_code == 200, response.text
        assert response.json()['features']
        assert response.json()['debug']['source'] == 'postgis'


def test_nearby_boundary_inside_and_just_outside(env):
    """ST_DWithin boundary: A just inside 1 km, B just outside — distances are
    great-circle km and the radius is honoured to the sub-kilometre."""
    _staff(env)
    with _client(env) as client:
        ra, adv_a = _report_with_published_advisory(env, client, *NEAR_A)
        rb, adv_b = _report_with_published_advisory(env, client, *NEAR_B)
        f = _nearby(client, radius_km=5).json()['features']
        by_id = {x['id']: x for x in f}
        assert adv_a['id'] in by_id and adv_b['id'] in by_id
        a_dist = by_id[adv_a['id']]['properties']['distance_km']
        b_dist = by_id[adv_b['id']]['properties']['distance_km']
        assert a_dist < 1.0 < b_dist
        assert a_dist < 0.7 and b_dist > 0.9  # ≈0.55 km and ≈1.11 km
        within_1km = _nearby(client, radius_km=1).json()['features']
        inside_ids = {x['id'] for x in within_1km}
        assert adv_a['id'] in inside_ids and adv_b['id'] not in inside_ids


def test_nearby_only_reports_with_active_published_advisory(env):
    """Draft, submitted, verified-but-unpublished and expired advisories are all
    invisible; the gate matches the public advisory list."""
    _staff(env)
    with _client(env) as client:
        # 1) submitted only (no review, no advisory)
        login(client, 'reporter_pub@example.org')
        rep_submitted = new_report(client, area='community-a', lat=0.15, lon=31.105, share=True)
        submit(client, rep_submitted['id'])
        # 2) verified but never published
        rep_verified = new_report(client, area='community-a', lat=0.15, lon=31.108, share=True)
        submit(client, rep_verified['id'])
        login(client, 'pub_reviewer@example.org')
        review(client, rep_verified['id'], decision='verified', notes='Verified.')
        created = client.post('/api/v1/advisories', json={
            'report_id': rep_verified['id'], 'title': 'Draft only',
            'body': 'Never published by anyone.', 'source': 'Verified field report',
            'expires_at': (datetime.now(timezone.utc)
                           + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')})
        assert created.status_code == 201, created.text
        # 3) published then expired
        ra, adv_a = _report_with_published_advisory(env, client, *NEAR_A)
        with env['engine'].begin() as conn:
            conn.execute(text(
                "UPDATE advisories SET expires_at = '2020-01-01T00:00:00Z'"))
        published_ids = {f['id'] for f in _nearby(client, radius_km=20).json()['features']}
        assert rep_submitted['id'] not in published_ids
        assert rep_verified['id'] not in published_ids
        assert adv_a['id'] not in published_ids  # advisory exists but is expired
        # 4) positive control: an active published advisory does appear
        rb, adv_b = _report_with_published_advisory(env, client, *NEAR_B)
        published_ids = {f['id'] for f in _nearby(client, radius_km=20).json()['features']}
        assert adv_b['id'] in published_ids


def test_nearby_category_filter_and_radius_cap(env):
    _staff(env)
    with _client(env) as client:
        rw, adv_w = _report_with_published_advisory(env, client, *NEAR_A, category='wildlife')
        rf, adv_f = _report_with_published_advisory(env, client, *NEAR_B, category='flood')
        wildlife = _nearby(client, radius_km=10, category='wildlife').json()['features']
        assert {f['id'] for f in wildlife} == {adv_w['id']}
        # radius above the 50 km cap and unknown categories are rejected
        assert _nearby(client, radius_km=50.5).status_code == 422
        assert _nearby(client, radius_km=0.5).status_code == 422
        assert _nearby(client, category='banana').status_code == 422
        assert client.get('/api/v1/public/nearby', params={'lat': 91, 'lon': 0, 'radius_km': 5}).status_code == 422


# --------------------------------------------------------------------------- #
# Privacy: generalisation is never bypassed; nothing sensitive is exposed
# --------------------------------------------------------------------------- #
def test_nearby_serves_only_the_generalised_point_never_the_exact_fix(env):
    """A reporter who shares an exact GPS fix still only ever surfaces the ~1 km
    grid point — the same guarantee as the out-of-area staff redaction."""
    _staff(env)
    with _client(env) as client:
        rep, adv = _report_with_published_advisory(env, client, *NEAR_A)
        payload = _nearby(client, radius_km=5).json()
        feat = next(f for f in payload['features'] if f['id'] == adv['id'])
        served = feat['geometry']['coordinates']
        assert served == list(spatial.grid_point(NEAR_A[0], NEAR_A[1]))
        assert served != [NEAR_A[0], NEAR_A[1]]  # the precise fix is never the served point
        assert feat['properties']['location_precision'] == 'generalised'


FORBIDDEN_KEYS = (
    'code', 'title', 'description', 'species', 'consent', 'client_id',
    'assignee_id', 'is_owner', 'evidence', 'timeline', 'reviews', 'version',
    'share_location', 'report_id', 'body', 'source', 'author', 'publisher',
    'email', 'message', 'notes', 'reason', 'retraction_reason',
)


def test_nearby_payload_carries_no_sensitive_fields(env):
    _staff(env)
    with _client(env) as client:
        _report_with_published_advisory(env, client, *NEAR_A)
        payload = _nearby(client, radius_km=5).json()
        assert set(payload) == {'type', 'features', 'clusters', 'areas',
                                'location_policy', 'attribution', 'debug'}
        assert payload['type'] == 'FeatureCollection'
        for feat in payload['features']:
            props = feat['properties']
            assert set(props) == {'id', 'kind'} | set(CASE_FIELDS)
            assert props['kind'] == 'case'
            assert len(feat['geometry']['coordinates']) == 2
        dumped = repr(payload['features'])
        for key in FORBIDDEN_KEYS:
            assert key not in dumped, f'forbidden key leaked: {key}'
        # debug.source is a deliberate honesty marker; the feature payload itself
        # must still never name a report (report ids stay server-side)
        assert 'report_id' not in repr(payload['features'])


def test_nearby_empty_state_far_from_cases(env):
    _staff(env)
    with _client(env) as client:
        _report_with_published_advisory(env, client, *NEAR_A)
        payload = _nearby(client, lon=NOWHERE_LON, lat=NOWHERE_LAT, radius_km=10).json()
        assert payload['features'] == []


# --------------------------------------------------------------------------- #
# Server cache honesty (mirrors /map), rate limiting
# --------------------------------------------------------------------------- #
def test_nearby_shared_grid_cache_and_honest_source(env, monkeypatch):
    class _Settings:
        osm_attribution = '© OpenStreetMap contributors, ODbL'
        cache_ttl_seconds = 60
    monkeypatch.setattr('app.public.get_settings', lambda: _Settings())
    with _client(env) as client:
        first = _nearby(client).json()
        second = _nearby(client).json()
        assert first['debug']['source'] == 'postgis'
        assert second['debug']['source'] == 'cache'
        assert first['features'] == second['features']


def test_nearby_rate_limit_enforced(env, monkeypatch):
    from app.security import limiter as app_limiter
    app_limiter.memory.clear()
    try:
        class _Dev:
            app_env = 'development'
            redis_url = ''
        monkeypatch.setattr('app.security.get_settings', lambda: _Dev())
        monkeypatch.setattr('app.public.NEARBY_LIMIT', 3)
        with _client(env) as client:
            url = '/api/v1/public/nearby?lat=0.15&lon=31.110&radius_km=5'
            codes = [client.get(url).status_code for _ in range(4)]
        assert codes[:3] == [200, 200, 200], codes
        assert codes[3] == 429, codes
    finally:
        app_limiter.memory.clear()


# --------------------------------------------------------------------------- #
# /public/places: areas_osm + gazetteer combined
# --------------------------------------------------------------------------- #
def test_places_search_combines_osm_areas_and_gazetteer(env):
    with env['engine'].begin() as conn:
        conn.execute(text(
            "INSERT INTO places (osm_type, osm_id, name, kind, geom) "
            "VALUES ('node', 9001, 'Fixture Village', 'village', "
            "ST_SetSRID(ST_MakePoint(31.19, 0.19), 4326))"))
    with _client(env) as client:
        body = client.get('/api/v1/public/places', params={'q': 'fixt', 'limit': 10})
        assert body.status_code == 200, body.text
        items = body.json()['items']
        kinds = {i['kind'] for i in items}
        by_kind = {i['kind']: i for i in items}
        assert kinds == {'area', 'place'}
        assert by_kind['place']['name'] == 'Fixture Village'
        assert (by_kind['place']['lon'], by_kind['place']['lat']) == (31.19, 0.19)
        area_names = {i['name'] for i in items if i['kind'] == 'area'}
        assert {'Fixture District', 'Fixture National Park'} <= area_names
        # village-only query hits the gazetteer row (name-prefix match sorts first)
        village = client.get('/api/v1/public/places', params={'q': 'vill'}).json()['items']
        assert [i['name'] for i in village] == ['Fixture Village']
        # wildcard input is neutralised, not passed to ILIKE
        assert client.get('/api/v1/public/places', params={'q': '%%'}).json()['items'] == []


# --------------------------------------------------------------------------- #
# /public/preferred-location: coarsening, round-trip, clear
# --------------------------------------------------------------------------- #
def test_preferred_location_is_coarsened_on_server(env):
    cid = 'test-client-' + uuid.uuid4().hex[:10]
    precise_lon, precise_lat = 31.1234567, 0.2345678
    glon, glat = spatial.grid_point(precise_lon, precise_lat)
    assert (glon, glat) != (precise_lon, precise_lat)
    with _client(env) as client:
        saved = client.post('/api/v1/public/preferred-location', json={
            'client_id': cid, 'latitude': precise_lat, 'longitude': precise_lon,
            'name': 'Fixture home'})
        assert saved.status_code == 201, saved.text
        assert (saved.json()['longitude'], saved.json()['latitude']) == (glon, glat)
        s = sessionmaker(bind=env['engine'], expire_on_commit=False)()
        try:
            stored = s.execute(text(
                'SELECT latitude, longitude FROM public_preferred_locations '
                'WHERE client_id = :c'), {'c': cid}).first()
        finally:
            s.close()
        assert stored == (glat, glon)  # only the coarsened point is persisted
        got = client.get('/api/v1/public/preferred-location',
                         params={'client_id': cid}).json()
        assert (got['longitude'], got['latitude']) == (glon, glat)
        assert got['precision'] == 'generalised'
        # overwrite keeps coarsening (upsert path)
        saved2 = client.post('/api/v1/public/preferred-location', json={
            'client_id': cid, 'latitude': 0.999999, 'longitude': 31.999999})
        assert saved2.json()['longitude'] == spatial.grid_value(31.999999)
        assert client.delete('/api/v1/public/preferred-location',
                             params={'client_id': cid}).status_code == 204
        assert client.get('/api/v1/public/preferred-location',
                          params={'client_id': cid}).status_code == 404


# --------------------------------------------------------------------------- #
# SQLite harness fallback: haversine + same advisory gate
# --------------------------------------------------------------------------- #
def test_nearby_sqlite_fallback_uses_haversine_and_the_same_gate():
    from fastapi.testclient import TestClient
    from app import db as db_module
    from app.main import app
    from app.models import Area, Advisory, Report, User
    from app.security import hash_password
    from sqlalchemy.pool import StaticPool

    # In-memory SQLite needs a single shared connection (StaticPool); the
    # default pool hands each session a private in-memory database.
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    from app.db import Base as AppBase
    AppBase.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    s = maker()
    try:
        area = Area(id='community-a', name='Community A',
                    latitude=0.15, longitude=31.105, radius_km=10)
        owner = User(id='u-r', email='r@x.io', name='R', password_hash=hash_password('x'),
                     roles=['reporter'])
        rep = Report(id='rep-1', client_id='cl-1', code='R-001', owner_id='u-r',
                     area_id='community-a', category='wildlife', title='Sighting',
                     description='A report.', species='Elephant', observed_at='2026-01-01T10:00:00Z',
                     latitude=0.15, longitude=31.105, share_location=True, consent=True,
                     state='verified')
        adv = Advisory(id='adv-1', report_id='rep-1', area_id='community-a', author_id='u-r',
                       category='wildlife', title='Advisory', body='Body', source='Report',
                       state='published',
                       expires_at=(datetime.now(timezone.utc)
                                   + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        s.add_all([area, owner, rep, adv])
        s.commit()
    finally:
        s.close()
    original = db_module.SessionLocal
    db_module.SessionLocal = maker
    try:
        with TestClient(app) as client:
            inside = client.get('/api/v1/public/nearby',
                                params={'lat': 0.15, 'lon': 31.105, 'radius_km': 1})
            assert inside.status_code == 200, inside.text
            body = inside.json()
            assert body['debug']['source'] == 'sqlite'
            assert len(body['features']) == 1
            outside = client.get('/api/v1/public/nearby',
                                 params={'lat': 0.15, 'lon': 31.3, 'radius_km': 1})
            assert outside.json()['features'] == []
            # same visibility gate: a draft advisory never surfaces
            s = maker()
            try:
                s.query(Advisory).filter_by(id='adv-1').update({'state': 'draft'})
                s.commit()
            finally:
                s.close()
            hidden = client.get('/api/v1/public/nearby',
                                params={'lat': 0.15, 'lon': 31.105, 'radius_km': 1})
            assert hidden.json()['features'] == []
    finally:
        db_module.SessionLocal = original