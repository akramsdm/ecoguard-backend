"""Step 5 map endpoint: viewport bbox filtering, per-user caching, low-zoom clusters.

Like test_spatial_authorization.py these need a live PostGIS database (scratch
pattern) because the filtering is spatial. The endpoint still falls back to the
legacy non-spatial path on SQLite, which the rest of the suite keeps covering.
"""
from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from test_spatial_authorization import (
    _admin_engine, _scratch_url, _make_alembic_cfg, _postgres_available,
    _create_scratch, _drop_scratch, _clean, _osm_ids, _seed_legacy_areas,
    _Seeder, _client, login, new_report, submit,
    PARK_LON, PARK_LAT, DISTRICT_LON, DISTRICT_LAT, NOWHERE_LON, NOWHERE_LAT,
)


@pytest.fixture(scope='module')
def scratch_pg():
    if not _postgres_available():
        pytest.skip('Local Postgres is unavailable for map viewport tests')
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
    yield {'engine': scratch_pg['engine'], 'ids': ids,
           'seed': _Seeder(scratch_pg['engine'], ids)}


def _reviewer(env) -> None:
    """A reviewer assigned to the fixture park, who sees park-contained reports full
    and district-only/outside reports redacted."""
    env['seed'].add_user('reviewer@example.org', 'Reviewer', ['reviewer'],
                         osm_ids=[env['ids']['park']])


def test_bbox_excludes_out_of_viewport_reports(env):
    _reviewer(env)
    with _client(env) as client:
        login(client, 'reviewer@example.org')
        a = new_report(client, area='community-a', lat=PARK_LAT, lon=PARK_LON, share=True)
        submit(client, a['id'])
        b = new_report(client, area='community-b', lat=DISTRICT_LAT, lon=DISTRICT_LON, share=False)
        submit(client, b['id'])
        c = new_report(client, area='wetland-a', lat=NOWHERE_LAT, lon=NOWHERE_LON, share=True)
        submit(client, c['id'])

        # A viewport covering all three returns them individually at high zoom,
        # with no server-side clustering and the overlay areas attached.
        full = client.get('/api/v1/map?view=staff&bbox=30.9,-0.1,33.2,1.2&zoom=14').json()
        assert full['debug']['clustered'] is False
        assert {f['id'] for f in full['features']} == {a['id'], b['id'], c['id']}
        by_id = {f['id']: f for f in full['features']}
        assert by_id[a['id']]['properties']['precision'] == 'private-evidence'
        assert by_id[a['id']]['properties'].get('redacted') is not True
        # Assigned park polygon is emphasised; district-only containment is muted.
        park = next(x for x in full['areas'] if x['id'] == env['ids']['park'])
        assert park['assigned'] is True and park['geometry'] is not None
        assert any(x['assigned'] is False for x in full['areas'])

        # A tight viewport around the park drops the district and outside points.
        view = client.get('/api/v1/map?view=staff&bbox=31.05,0.1,31.15,0.2&zoom=14').json()
        assert {f['id'] for f in view['features']} == {a['id']}


def test_clustering_aggregates_at_low_zoom(env):
    _reviewer(env)
    with _client(env) as client:
        login(client, 'reviewer@example.org')
        made = []
        for lon, lat in ((31.02, 0.12), (31.08, 0.16), (31.12, 0.14)):
            r = new_report(client, area='community-a', lat=lat, lon=lon, share=True)
            submit(client, r['id'])
            made.append(r['id'])

        low = client.get('/api/v1/map?view=staff&bbox=31.0,0.1,31.2,0.2&zoom=6').json()
        assert low['debug']['clustered'] is True
        assert low['features'] == []          # every point was absorbed
        assert len(low['clusters']) == 1
        cl = low['clusters'][0]
        assert cl['properties']['count'] == 3
        assert cl['properties']['categories'] == {'wildlife': 3}
        # The aggregate centroid is snapped to the public generalisation grid.
        glon, glat = cl['geometry']['coordinates']
        assert (glon, glat) == (round(glon / 0.00899) * 0.00899,
                                round(glat / 0.00899) * 0.00899)

        high = client.get('/api/v1/map?view=staff&bbox=31.0,0.1,31.2,0.2&zoom=14').json()
        assert high['debug']['clustered'] is False
        assert {f['id'] for f in high['features']} == set(made)


def test_clusters_leave_singletons_as_features(env):
    _reviewer(env)
    with _client(env) as client:
        login(client, 'reviewer@example.org')
        near = new_report(client, area='community-a', lat=0.15, lon=31.10, share=True)
        submit(client, near['id'])
        near2 = new_report(client, area='community-a', lat=0.16, lon=31.11, share=True)
        submit(client, near2['id'])
        far = new_report(client, area='wetland-a', lat=0.3, lon=34.5, share=True)
        submit(client, far['id'])
        # One dense cell (2) plus one singleton in the same low-zoom viewport.
        body = client.get('/api/v1/map?view=staff&bbox=30.5,0.0,35.0,0.6&zoom=6').json()
        assert any(c['properties']['count'] == 2 for c in body['clusters'])
        assert any(f['id'] == far['id'] for f in body['features'])


def test_debug_source_is_honest(env, monkeypatch):
    """debug.source must say 'db' for a fresh computation and 'cache' only for a
    real cache read-back -- the label is read before the loader is consulted.
    The cache layer is simulated with a dict so the test is deterministic
    whether or not Redis is reachable from the test process."""
    from app.cache import cache
    _reviewer(env)
    store: dict = {}
    monkeypatch.setattr(cache, 'get', lambda key, _s=store: _s.get(key, None))
    monkeypatch.setattr(cache, 'set', lambda key, value, ttl, _s=store: _s.__setitem__(key, value))
    with _client(env) as client:
        login(client, 'reviewer@example.org')
        # Fixed distinctive viewport: no pre-existing entry can collide.
        url = '/api/v1/map?view=staff&bbox=33.100,0.100,33.101,0.101&zoom=14'
        fresh = client.get(url).json()
        assert fresh['debug']['source'] == 'db'
        assert fresh['debug']['cache_key'].startswith('map:staff:')
        hit = client.get(url).json()
        assert hit['debug']['source'] == 'cache'


def test_map_cache_key_includes_dimensions(env, monkeypatch):
    from app.cache import cache
    _reviewer(env)
    keys: list[str] = []

    def fake_cached(key, ttl, loader):
        keys.append(key)
        return loader()

    monkeypatch.setattr(cache, 'cached', fake_cached)
    with _client(env) as client:
        login(client, 'reviewer@example.org')
        staff = client.get('/api/v1/map?view=staff&bbox=29,0,33,2&zoom=12&category=wildlife')
        assert staff.status_code == 200
        community = client.get('/api/v1/map?view=community&bbox=29,0,33,2&zoom=7&category=flood')
        assert community.status_code == 200

    sk = [k for k in keys if k.startswith('map:staff:')]
    assert sk, 'staff map never consulted the cache'
    assert ':z12:' in sk[0] and 'wildlife' in sk[0] and '29.000,0.000,33.000,2.000' in sk[0]
    ck = [k for k in keys if k.startswith('map:community:')]
    assert ck and ck[0] == 'map:community:flood:z7:29.000,0.000,33.000,2.000'


def test_gist_indexes_exist_for_map_geometry(env):
    """Step 4 asked us to verify, not re-add: the GiST indexes the bbox queries
    lean on must already exist from migrations 0003/0004."""
    with env['engine'].connect() as conn:
        gist = {r[0] for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND indexdef ILIKE '%USING gist%'"))}
    assert {'ix_reports_location_geom', 'ix_reports_public_geom'} <= gist
    for col in ('geom', 'centroid', 'bbox', 'simplified_geom'):
        assert f'ix_areas_osm_{col}' in gist


def test_community_map_shows_all_cases_at_area_centroids(env):
    """QA finding: reporter-facing maps showed only advisories, never the
    cases staff could see. The signed-in community view now pins every
    non-draft report at its legacy area centroid: a shared precise position
    never leaks, drafts stay private, and no case code is exposed."""
    _reviewer(env)
    with _client(env) as client:
        login(client, 'reviewer@example.org')
        # A precise, shared report. The walk happened at NOWHERE, but the
        # community pin must be community-a's centroid (PARK point) instead.
        case = new_report(client, area='community-a', lat=NOWHERE_LAT, lon=NOWHERE_LON, share=True)
        submit(client, case['id'])
        # A draft must never appear on the community map.
        draft = new_report(client, area='wetland-a', lat=DISTRICT_LAT, lon=DISTRICT_LON, share=False)
        # A submitted case whose precise point is INSIDE the viewport but whose
        # centroid (community-b) is not must stay excluded: the bbox filter
        # runs on the displayed centroid, never on the private position.
        far = new_report(client, area='community-b', lat=PARK_LAT, lon=PARK_LON, share=True)
        submit(client, far['id'])

        # Tight park viewport: community-a's centroid (the PARK point) is
        # inside; the other fixture centroids are outside.
        body = client.get('/api/v1/map?view=community&bbox=31.05,0.10,31.15,0.20&zoom=14').json()
        kinds = {f['id']: f['properties']['kind'] for f in body['features']}
        assert kinds.get(draft['id']) is None, 'drafts must stay private on the community map'
        assert kinds.get(far['id']) is None, 'exclusion runs on the displayed centroid, not the private point'
        assert kinds.get(case['id']) == 'report', 'open cases must appear on the community map'

        feat = next(f for f in body['features'] if f['id'] == case['id'])
        # Pinned at the legacy area centroid, never at the precise NOWHERE fix.
        assert tuple(feat['geometry']['coordinates']) == (PARK_LON, PARK_LAT)
        assert feat['properties']['precision'] == 'community-centroid'
        assert feat['properties']['state'] == 'submitted'
        assert 'code' not in feat['properties']
        assert 'redacted' not in feat['properties']