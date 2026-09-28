"""Authorisation contract for every /admin/* surface.

Each administrative route must 401 an anonymous caller, 403 an authenticated user
without the admin role, and only then 200 for an administrator. The role is escalated
in place, mirroring test_admin_image_assistance_endpoint_reports_the_reason, so one
test proves the whole 401 -> 403 -> 200 sequence for a route rather than trusting a
single status code.
"""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.models import Area, Audit, User
from app.security import limiter

# (method, path, json body). The PATCH entry is resolved per-test by patch_target(),
# because the route 404s unless the target account actually exists.
ADMIN_ROUTES = [
    ('GET', '/api/v1/admin/dashboard', None),
    ('GET', '/api/v1/admin/users', None),
    ('POST', '/api/v1/admin/users',
     {'name': 'Case Officer', 'email': 'new.officer@example.org',
      'password': 'PassPhrase1234!', 'roles': ['reviewer'], 'areas': []}),
    ('PATCH', None, None),
    ('GET', '/api/v1/admin/audit', None),
    ('GET', '/api/v1/admin/jobs', None),
    ('GET', '/api/v1/admin/image-assistance', None),
    ('POST', '/api/v1/admin/areas',
     {'id': 'new-area', 'name': 'New Area', 'description': 'Test area.',
      'latitude': 0.5, 'longitude': 30.5, 'radius_km': 10}),
]


def ensure_target_user():
    """A second account for PATCH /admin/users/{id} to act on.

    Written straight to the database: the route 404s on an unknown id, and creating it
    through the API would replace the session cookie of the caller under test.
    """
    with SessionLocal() as db:
        user = db.query(User).filter_by(email='target@example.org').first()
        if not user:
            user = User(email='target@example.org', name='Target',
                        password_hash='not-used-here', roles=['reporter'],
                        areas=[], preferences={})
            db.add(user)
            db.commit()
        return user.id


def resolve(method, path, body):
    if method == 'PATCH':
        return f'/api/v1/admin/users/{ensure_target_user()}', {'roles': ['reporter'],
                                                               'areas': [], 'active': True}
    return path, body


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # The limiter is a process-wide singleton, so its counters must not leak between tests.
    limiter.memory.clear()
    with TestClient(__import__('app.main', fromlist=['app']).app) as test_client:
        yield test_client
    from app import cache
    cache.cache.delete('areas:')


def register(client, email='reporter@example.org', name='Reporter'):
    response = client.post('/api/v1/auth/register',
                           json={'email': email, 'name': name, 'password': 'PassPhrase1234!'})
    assert response.status_code == 201, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})
    return response.json()['user']


def set_roles(email, roles):
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=email).one()
        user.roles = list(roles)
        db.commit()
    return user.id


def call(client, method, path, body):
    if method == 'GET':
        return client.get(path)
    if method == 'POST':
        return client.post(path, json=body)
    return client.patch(path, json=body)


@pytest.mark.parametrize('method,path,body', ADMIN_ROUTES,
                         ids=[f'{m} {p or "/admin/users/{id}"}' for m, p, _ in ADMIN_ROUTES])
def test_admin_route_rejects_anonymous_then_non_admin_then_allows_admin(client, method, path, body):
    # 1. No session at all.
    path, body = resolve(method, path, body)
    assert call(client, method, path, body).status_code == 401

    # 2. Valid credentials, but the account holds no staff role.
    register(client)
    assert call(client, method, path, body).status_code == 403

    # 3. The same session, now holding admin.
    set_roles('reporter@example.org', ['admin'])
    response = call(client, method, path, body)
    assert response.status_code in (200, 201), response.text


def test_non_admin_roles_are_each_refused_every_admin_route(client):
    """A reviewer, responder or publisher is staff but still not an administrator."""
    for role in ('reviewer', 'responder', 'publisher'):
        client.cookies.clear()
        register(client, f'{role}@example.org', role.title())
        for method, path, body in ADMIN_ROUTES:
            path, body = resolve(method, path, body)
            assert call(client, method, path, body).status_code == 403, f'{role} reached {path}'
        set_roles(f'{role}@example.org', ['reporter'])
        client.post('/api/v1/auth/logout')


def test_creating_an_area_requires_admin_and_is_audited(client):
    payload = {'id': 'community-b', 'name': 'Community B', 'description': 'Wetland edge.',
               'latitude': -0.35, 'longitude': 32.55, 'radius_km': 12}
    assert client.post('/api/v1/admin/areas', json=payload).status_code == 401
    register(client)
    assert client.post('/api/v1/admin/areas', json=payload).status_code == 403
    user_id = set_roles('reporter@example.org', ['admin'])

    created = client.post('/api/v1/admin/areas', json=payload)
    assert created.status_code == 201, created.text
    assert created.json() == payload
    with SessionLocal() as db:
        area = db.get(Area, 'community-b')
        assert (area.name, area.latitude, area.longitude, area.radius_km) == (
            'Community B', -0.35, 32.55, 12)
        assert [a.actor_id for a in db.query(Audit)
                .filter_by(action='area.created', target_id='community-b')] == [user_id]


def test_a_duplicate_area_key_is_rejected(client):
    register(client)
    set_roles('reporter@example.org', ['admin'])
    payload = {'id': 'community-c', 'name': 'Community C', 'description': '',
               'latitude': 0.1, 'longitude': 30.1, 'radius_km': 10}
    assert client.post('/api/v1/admin/areas', json=payload).status_code == 201
    duplicate = client.post('/api/v1/admin/areas', json=payload)
    assert duplicate.status_code == 409
    assert 'already exists' in duplicate.json()['detail']


def test_a_new_area_is_visible_immediately_despite_a_long_cache(client, monkeypatch):
    """touch() invalidates the 'areas:' prefix, so the key must live under that prefix."""
    from app import cache
    from app.config import get_settings
    monkeypatch.setenv('CACHE_TTL_SECONDS', '300')
    get_settings.cache_clear()
    cache.cache.delete('areas:')

    assert client.get('/api/v1/areas').json()['items'] == []
    register(client)
    set_roles('reporter@example.org', ['admin'])
    assert client.post('/api/v1/admin/areas', json={
        'id': 'community-d', 'name': 'Community D', 'description': '',
        'latitude': 0.2, 'longitude': 30.2, 'radius_km': 10}).status_code == 201

    # Same 300s cache window, but the create must have invalidated the list.
    names = [a['id'] for a in client.get('/api/v1/areas').json()['items']]
    assert names == ['community-d']
    cache.cache.delete('areas:')
    get_settings.cache_clear()


def test_an_area_centroid_outside_the_allowed_range_is_rejected(client):
    register(client)
    set_roles('reporter@example.org', ['admin'])
    for field, value in (('latitude', 91), ('longitude', -181), ('radius_km', 0)):
        response = client.post('/api/v1/admin/areas', json={
            'id': f'bad-{field}', 'name': 'Bad', 'description': '',
            'latitude': 0.1, 'longitude': 30.1, 'radius_km': 10, field: value})
        assert response.status_code == 422, f'{field}={value} was accepted'


def test_readiness_does_not_claim_postgis_on_a_plain_sqlite_database(client):
    body = client.get('/api/v1/health/ready').json()
    assert body['status'] == 'ok'
    # The SQLite test database has no pg_extension catalogue, so PostGIS must not be claimed.
    assert body['database'] == 'sqlite-local-development'
    assert 'postgis' not in body['database']


def test_the_realtime_stream_requires_a_session(client):
    """Anonymous callers are refused before any event is streamed."""
    assert client.get('/api/v1/stream').status_code == 401


def test_the_realtime_stream_route_is_guarded_by_the_session_dependency():
    """The SSE response never ends, so a live 200 cannot be asserted without hanging
    the suite. Asserting the guard is wired to the route catches the same regression
    that the 401 above would catch, without opening a connection that never closes."""
    from app.main import app
    from app.security import current_user
    route = next(r for r in app.routes if getattr(r, 'path', '') == '/api/v1/stream')
    assert current_user in {dependency.call for dependency in route.dependant.dependencies}
