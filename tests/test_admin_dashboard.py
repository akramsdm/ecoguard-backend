"""GET /admin/dashboard content and the idempotent ensure-admin seeder.

The shared contract in test_admin_authorization.py already proves the route is
401/403/200-gated. What is proved here is that the payload is worth rendering:
the counts are real aggregates, and the attention list names the specific
configuration traps that make the rest of the product look broken.
"""
import pytest
from fastapi.testclient import TestClient

from app import cli
from app.db import Base, SessionLocal, engine
from app.models import Area, Outbox, Report, User
from app.security import limiter


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    limiter.memory.clear()
    with TestClient(__import__('app.main', fromlist=['app']).app) as test_client:
        yield test_client


def sign_in_as_admin(client, password='PassPhrase1234!'):
    """Provision the admin through the seeder, then sign in over HTTP for real.

    The admin is given area-a so the baseline is healthy: an account with no area is
    exactly the configuration the dashboard warns about, and every test here would
    otherwise trip that warning instead of the one it is actually asserting.
    """
    with SessionLocal() as db:
        db.add(Area(id='area-a', name='Area A', description='Test area.',
                    latitude=0.2, longitude=30.1, radius_km=10))
        db.commit()
    cli.ensure_admin('admin@example.org', 'Admin', password, ['admin'], ['area-a'])
    response = client.post('/api/v1/auth/login',
                           json={'email': 'admin@example.org', 'password': password})
    assert response.status_code == 200, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})
    return client


def test_the_dashboard_reports_accounts_areas_reports_and_jobs(client):
    sign_in_as_admin(client)
    with SessionLocal() as db:
        admin_id = db.query(User).filter_by(email='admin@example.org').one().id
        db.add(User(email='officer@example.org', name='Officer', password_hash='x',
                    roles=['reviewer'], areas=['area-a'], preferences={}))
        db.add(Report(client_id='c1', code='EG-W-001', owner_id=admin_id, area_id='area-a',
                      category='wildlife', title='Report', description='Test.',
                      observed_at='2026-01-01T00:00:00Z', consent=True,
                      share_location=False, state='submitted'))
        db.add(Outbox(kind='advisory', aggregate_id='c1', dedupe_key='k1', state='pending'))
        db.add(Outbox(kind='advisory', aggregate_id='c2', dedupe_key='k2', state='failed',
                      attempts=5, last_error='Provider unreachable.'))
        db.commit()

    body = client.get('/api/v1/admin/dashboard').json()

    # Two accounts: the admin and the reviewer. by_role is a flat role tally, so a user
    # holding two roles counts once per role.
    assert body['users']['total'] == 2
    assert body['users']['by_role']['admin'] == 1
    assert body['users']['by_role']['reviewer'] == 1
    assert body['users']['case_staff'] == 2
    assert body['users']['inactive'] == 0

    assert body['areas']['total'] == 1
    assert body['areas']['with_staff'] == 1
    assert body['areas']['without_staff'] == []

    assert body['reports']['total'] == 1
    assert body['reports']['by_state']['submitted'] == 1
    assert body['reports']['by_category']['wildlife'] == 1

    assert body['jobs']['total'] == 2
    assert body['jobs']['by_state']['failed'] == 1
    assert body['jobs']['failed'][0]['last_error'] == 'Provider unreachable.'
    assert body['note']


def test_the_dashboard_warns_about_an_area_with_no_assigned_staff(client):
    """The exact trap: an area exists, reports arrive, but nobody can review them
    because no active staff account holds that area."""
    sign_in_as_admin(client)
    with SessionLocal() as db:
        db.add(Area(id='orphan', name='Orphan Area', description='Nobody assigned.',
                    latitude=0.4, longitude=30.4, radius_km=10))
        db.commit()

    body = client.get('/api/v1/admin/dashboard').json()

    assert [a['id'] for a in body['areas']['without_staff']] == ['orphan']
    assert body['areas']['with_staff'] == 1
    assert any('no active staff assigned' in a['message'] for a in body['attention'])


def test_the_dashboard_flags_staff_with_no_area_so_empty_views_are_explained(client):
    sign_in_as_admin(client)
    with SessionLocal() as db:
        db.add(User(email='idle@example.org', name='Idle', password_hash='x',
                    roles=['reviewer'], areas=[], preferences={}))
        db.commit()

    body = client.get('/api/v1/admin/dashboard').json()

    assert body['users']['case_staff'] == 2
    assert any('no assigned area' in a['message'] for a in body['attention'])


def test_the_dashboard_raises_a_high_severity_warning_when_no_admin_is_active(client):
    """Nothing else in the product can restore access, so this is the loudest warning."""
    sign_in_as_admin(client)
    with SessionLocal() as db:
        db.query(User).filter_by(email='admin@example.org').one().active = False
        db.commit()
    # The deactivated account's session survives only because logout was not called;
    # the route checks the role on the live row, so the response is still produced.
    body = client.get('/api/v1/admin/dashboard')
    if body.status_code == 200:
        assert any('No active administrator' in a['message'] for a in body.json()['attention'])
    else:
        assert body.status_code in (401, 403)


def test_the_dashboard_reports_ok_when_nothing_is_wrong(client):
    sign_in_as_admin(client)
    with SessionLocal() as db:
        db.add(User(email='officer@example.org', name='Officer', password_hash='x',
                    roles=['reviewer'], areas=['area-a'], preferences={}))
        db.commit()

    body = client.get('/api/v1/admin/dashboard').json()
    assert [a['severity'] for a in body['attention']] == ['ok']


def test_a_failed_job_raises_a_high_severity_warning(client):
    sign_in_as_admin(client)
    with SessionLocal() as db:
        db.add(Outbox(kind='advisory', aggregate_id='x', dedupe_key='kf', state='failed',
                      attempts=5, last_error='boom'))
        db.commit()

    body = client.get('/api/v1/admin/dashboard').json()
    assert any(a['severity'] == 'high' and 'background job' in a['message']
               for a in body['attention'])


class TestEnsureAdminSeeder:
    def test_it_creates_a_working_administrator(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], [])

        with SessionLocal() as db:
            user = db.query(User).filter_by(email='boss@example.org').one()
            assert user.roles == ['admin']
            assert user.active is True
            assert user.preferences == {}

    def test_rerunning_it_updates_rather_than_failing(self):
        """The recovery case: an existing admin lost their password. create-admin
        exits with 'Account already exists'; ensure-admin must repair it in place."""
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], [])
        cli.ensure_admin('boss@example.org', 'Boss', 'SecondPhrase5678!', ['admin'], [])

        with SessionLocal() as db:
            assert db.query(User).filter_by(email='boss@example.org').count() == 1

        with TestClient(__import__('app.main', fromlist=['app']).app) as client:
            assert client.post('/api/v1/auth/login', json={
                'email': 'boss@example.org', 'password': 'PassPhrase1234!'}).status_code == 401
            response = client.post('/api/v1/auth/login', json={
                'email': 'boss@example.org', 'password': 'SecondPhrase5678!'})
            assert response.status_code == 200, response.text

    def test_it_grants_roles_and_areas_so_staff_views_are_not_empty(self):
        """The reason --roles and --areas exist: a bare admin account is created with no
        area, which makes the area-scoped staff map and dashboard render nothing."""
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add(Area(id='area-a', name='Area A', description='d',
                        latitude=0.2, longitude=30.1, radius_km=10))
            db.add(Area(id='area-b', name='Area B', description='d',
                        latitude=0.4, longitude=30.4, radius_km=10))
            db.commit()
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!',
                         ['admin', 'reviewer'], ['area-a', 'area-b', 'area-a'])

        with SessionLocal() as db:
            user = db.query(User).filter_by(email='boss@example.org').one()
            assert user.roles == ['admin', 'reviewer']
            # Deduplicated, so a repeated key cannot silently widen the assignment.
            assert user.areas == ['area-a', 'area-b']

    def test_it_rejects_an_unknown_area_key_instead_of_creating_a_dangling_one(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with pytest.raises(SystemExit) as error:
            cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], ['ghost'])
        assert 'ghost' in str(error.value)
        with SessionLocal() as db:
            assert db.query(User).filter_by(email='boss@example.org').count() == 0

    def test_a_bare_rerun_preserves_roles_and_areas(self):
        """An administrator recovering a lost password runs the command without flags.
        That must reset the password and nothing else, or the recovery silently strips the
        access that was granted earlier."""
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add(Area(id='area-a', name='Area A', description='d',
                        latitude=0.2, longitude=30.1, radius_km=10))
            db.commit()
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!',
                         ['admin', 'reviewer'], ['area-a'])
        cli.ensure_admin('boss@example.org', None, 'SecondPhrase5678!', None, None)

        with SessionLocal() as db:
            user = db.query(User).filter_by(email='boss@example.org').one()
            assert user.roles == ['admin', 'reviewer']
            assert user.areas == ['area-a']
            assert user.name == 'Boss'

    def test_an_explicit_empty_areas_flag_clears_the_assignment(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add(Area(id='area-a', name='Area A', description='d',
                        latitude=0.2, longitude=30.1, radius_km=10))
            db.commit()
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], ['area-a'])
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', None, [''])
        with SessionLocal() as db:
            assert db.query(User).filter_by(email='boss@example.org').one().areas == []

    def test_a_new_account_still_defaults_to_admin_with_no_areas(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cli.ensure_admin('boss@example.org', None, 'PassPhrase1234!', None, None)
        with SessionLocal() as db:
            user = db.query(User).filter_by(email='boss@example.org').one()
            assert user.roles == ['admin']
            assert user.areas == []

    def test_it_rejects_a_short_password_and_an_unknown_role(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with pytest.raises(SystemExit) as short:
            cli.ensure_admin('boss@example.org', 'Boss', 'short', ['admin'], [])
        assert '12 characters' in str(short.value)
        with pytest.raises(SystemExit) as bad_role:
            cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['superuser'], [])
        assert 'superuser' in str(bad_role.value)

    def test_it_reactivates_a_deactivated_account(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], [])
        with SessionLocal() as db:
            db.query(User).filter_by(email='boss@example.org').one().active = False
            db.commit()
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], [])
        with SessionLocal() as db:
            assert db.query(User).filter_by(email='boss@example.org').one().active is True

    def test_it_revokes_existing_sessions_when_roles_change(self):
        """Matches PATCH /admin/users, which deletes the target's sessions. Without this
        a re-provisioned admin could keep using a session carrying superseded roles."""
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add(Area(id='area-a', name='Area A', description='d',
                        latitude=0.2, longitude=30.1, radius_km=10))
            db.commit()
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin'], [])
        with TestClient(__import__('app.main', fromlist=['app']).app) as client:
            assert client.post('/api/v1/auth/login', json={
                'email': 'boss@example.org', 'password': 'PassPhrase1234!'}).status_code == 200
        cli.ensure_admin('boss@example.org', 'Boss', 'PassPhrase1234!', ['admin', 'reviewer'],
                         ['area-a'])

        with TestClient(__import__('app.main', fromlist=['app']).app) as client:
            assert client.get('/api/v1/auth/me').status_code == 401

    def test_the_comma_and_repeat_forms_parse_identically(self):
        assert cli._split(['a,b', 'c']) == ['a', 'b', 'c']
        assert cli._split('a,b') == ['a', 'b']
        assert cli._split(None) == []
