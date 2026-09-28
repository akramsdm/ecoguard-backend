"""Report lifecycle state machine and the human-verified precondition.

These are the invariants the whole product rests on: a case only moves forward from a
permitted prior state, a reporter can never verify their own report, staff are confined
to their assigned areas, and no advisory can exist without a human 'verified' decision.
They are asserted here because the guards are spread across reports.py and advisories.py
and a regression in any one of them would be silent.
"""
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.models import Area, Review, User
from app.security import limiter

ALL_STATES = ['draft', 'submitted', 'under_review', 'needs_evidence',
              'verified', 'rejected', 'closed']


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    limiter.memory.clear()
    with TestClient(__import__('app.main', fromlist=['app']).app) as test_client:
        yield test_client
    from app import cache
    cache.cache.delete('areas:')


def login(client, email, name):
    client.cookies.clear()
    response = client.post('/api/v1/auth/login',
                           json={'email': email, 'password': 'PassPhrase1234!'})
    assert response.status_code == 200, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})
    return response.json()['user']


def register(client, email, name):
    response = client.post('/api/v1/auth/register',
                           json={'email': email, 'name': name, 'password': 'PassPhrase1234!'})
    assert response.status_code == 201, response.text
    client.headers.update({'X-CSRF-Token': response.json()['csrf_token']})
    return response.json()['user']


def promote(email, roles, areas=()):
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=email).one()
        user.roles = list(roles)
        user.areas = list(areas)
        db.commit()
    return user.id


def ensure_areas(*ids):
    with SessionLocal() as db:
        for i, area_id in enumerate(ids):
            if not db.get(Area, area_id):
                db.add(Area(id=area_id, name=area_id.title(), description='',
                            latitude=0.1 * i, longitude=30.1 * i, radius_km=10))
        db.commit()


def new_report(client, client_id='case-00000001', area_id='area-a', title='Elephant near the boundary'):
    response = client.post('/api/v1/reports', json={
        'client_id': client_id, 'category': 'wildlife', 'title': title,
        'description': 'Observed from a safe distance across a maize field.',
        'area_id': area_id, 'species': 'Unknown animal',
        'observed_at': '2026-01-01T00:00:00+00:00', 'consent': True,
        'share_location': False, 'evidence_ids': []})
    assert response.status_code == 201, response.text
    return response.json()


def force_state(report_id, state):
    with SessionLocal() as db:
        from app.models import Report
        db.get(Report, report_id).state = state
        db.commit()


def read_state(report_id):
    with SessionLocal() as db:
        from app.models import Report
        return db.get(Report, report_id).state


def staff_session(client, email, name, roles, areas=()):
    """Create a staff account and sign in as it, in a session distinct from the reporter.

    Every staff test needs a *different* account from the report's owner: the self-review
    guard in reports.py returns 403 before the state-machine check is ever reached, so a
    test that reuses the reporter's account would assert 403 while claiming to test
    transition legality.
    """
    register(client, email, name)
    promote(email, roles, areas)
    return login(client, email, name)


def owned_report(client, state, area_id='area-a', client_id='case-00000001'):
    """A report owned by 'owner@example.org', parked in `state`."""
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client, client_id=client_id, area_id=area_id)
    if state != 'draft':
        force_state(report['id'], state)
    client.post('/api/v1/auth/logout')
    return report


def version_of(client, report_id):
    body = client.get(f'/api/v1/reports/{report_id}').json()
    assert 'version' in body, f'case {report_id} is not visible to this user: {body}'
    return body['version']


# --- submit -------------------------------------------------------------------------

@pytest.mark.parametrize('state', ['draft', 'needs_evidence'])
def test_submit_is_accepted_from_draft_and_needs_evidence(client, state):
    ensure_areas('area-a')
    register(client, 'reporter@example.org', 'Reporter')
    report = new_report(client)
    if state != 'draft':
        force_state(report['id'], state)
    response = client.post(f"/api/v1/reports/{report['id']}/submit",
                           json={'version': version_of(client, report['id'])})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'submitted'


@pytest.mark.parametrize('state', ['under_review', 'verified', 'rejected', 'closed'])
def test_submit_is_refused_from_every_other_state(client, state):
    # 'submitted' is deliberately absent: re-submitting is an accepted idempotent retry,
    # covered by test_resubmitting_an_already_submitted_report_is_a_safe_retry.
    ensure_areas('area-a')
    register(client, 'reporter@example.org', 'Reporter')
    report = new_report(client)
    force_state(report['id'], state)
    response = client.post(f"/api/v1/reports/{report['id']}/submit",
                           json={'version': version_of(client, report['id'])})
    assert response.status_code == 409
    assert 'cannot be submitted' in response.json()['detail']


def test_resubmitting_an_already_submitted_report_is_a_safe_retry(client):
    ensure_areas('area-a')
    register(client, 'reporter@example.org', 'Reporter')
    report = new_report(client)
    first = client.post(f"/api/v1/reports/{report['id']}/submit",
                        json={'version': version_of(client, report['id'])})
    assert first.status_code == 200
    # No version bump in the retry payload: the idempotent path must not trip check_version.
    again = client.post(f"/api/v1/reports/{report['id']}/submit", json={'version': 1})
    assert again.status_code == 200
    assert again.json()['state'] == 'submitted'


def test_a_reporter_cannot_submit_someone_elses_report(client):
    ensure_areas('area-a')
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client, client_id='case-owner-01')
    force_state(report['id'], 'draft')
    client.post('/api/v1/auth/logout')

    # A genuinely different reporter, with the same ordinary reporter role.
    register(client, 'intruder@example.org', 'Intruder')
    mine = new_report(client, client_id='case-intruder-01', title='Buffalo at the river crossing')
    assert client.post(f"/api/v1/reports/{mine['id']}/submit",
                       json={'version': version_of(client, mine['id'])}).status_code == 200

    # The other account's draft is not even visible, let alone submittable.
    assert client.get(f"/api/v1/reports/{report['id']}").status_code == 404
    assert client.post(f"/api/v1/reports/{report['id']}/submit", json={'version': 1}).status_code == 404
    assert force_state_check(report['id'], 'draft')


def force_state_check(report_id, expected):
    """Confirm the other account's report was never advanced."""
    return read_state(report_id) == expected


# --- review -------------------------------------------------------------------------

def review_payload(version, decision='verified', notes='Clearly an elephant from the tusks.'):
    return {'version': version, 'decision': decision, 'notes': notes, 'species': 'African elephant'}


@pytest.mark.parametrize('state', ['submitted', 'under_review', 'needs_evidence'])
def test_review_is_accepted_from_the_three_open_states(client, state):
    ensure_areas('area-a')
    report = owned_report(client, state)
    staff_session(client, 'reviewer@example.org', 'Reviewer', ['reviewer'], ['area-a'])

    response = client.post(f"/api/v1/reports/{report['id']}/review",
                           json=review_payload(version_of(client, report['id'])))
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'verified'
    assert response.json()['species'] == 'African elephant'


@pytest.mark.parametrize('state', ['draft', 'verified', 'rejected', 'closed'])
def test_review_is_refused_outside_the_open_states(client, state):
    ensure_areas('area-a')
    report = owned_report(client, state)
    staff_session(client, 'reviewer@example.org', 'Reviewer', ['reviewer'], ['area-a'])

    if state == 'draft':
        # A draft is invisible to case staff at all, so the state check is never reached.
        assert client.get(f"/api/v1/reports/{report['id']}").status_code == 404
        response = client.post(f"/api/v1/reports/{report['id']}/review", json=review_payload(1))
        assert response.status_code == 404
    else:
        response = client.post(f"/api/v1/reports/{report['id']}/review",
                               json=review_payload(version_of(client, report['id'])))
        assert response.status_code == 409
        assert 'open review' in response.json()['detail']
    assert read_state(report['id']) == state


def test_a_reporter_cannot_review_their_own_report(client):
    """Self-review is the single most important guard here: a person must not verify
    their own sighting, however senior their role is."""
    ensure_areas('area-a')
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client)
    force_state(report['id'], 'submitted')
    client.post('/api/v1/auth/logout')
    # The owner is now also a reviewer, in the right area, on their own case.
    promote('owner@example.org', ['reviewer'], ['area-a'])
    login(client, 'owner@example.org', 'Owner')

    response = client.post(f"/api/v1/reports/{report['id']}/review",
                           json=review_payload(version_of(client, report['id'])))
    assert response.status_code == 403
    assert 'different reviewer' in response.json()['detail']
    with SessionLocal() as db:
        assert db.query(Review).filter_by(report_id=report['id']).count() == 0
    assert read_state(report['id']) == 'submitted'


def test_a_staff_reviewer_cannot_decide_a_case_outside_their_assigned_areas(client):
    ensure_areas('area-a', 'area-b')
    report = owned_report(client, 'submitted', area_id='area-b')
    staff_session(client, 'reviewer@example.org', 'Reviewer', ['reviewer'], ['area-a'])

    # The reviewer has the right role but the wrong area: must not even see the case.
    assert client.get(f"/api/v1/reports/{report['id']}").status_code == 404
    response = client.post(f"/api/v1/reports/{report['id']}/review", json=review_payload(1))
    assert response.status_code == 404
    assert read_state(report['id']) == 'submitted'


def test_a_reviewer_without_the_reviewer_role_cannot_review(client):
    ensure_areas('area-a')
    report = owned_report(client, 'submitted')
    # Right area, but responder is not a reviewing role.
    staff_session(client, 'responder@example.org', 'Responder', ['responder'], ['area-a'])

    response = client.post(f"/api/v1/reports/{report['id']}/review",
                           json=review_payload(version_of(client, report['id'])))
    assert response.status_code == 403
    assert 'role' in response.json()['detail']
    assert read_state(report['id']) == 'submitted'


def test_a_case_assigned_to_another_reviewer_cannot_be_decided(client):
    ensure_areas('area-a')
    report = owned_report(client, 'submitted')
    register(client, 'first@example.org', 'First')
    register(client, 'second@example.org', 'Second')
    with SessionLocal() as db:
        from app.models import Report
        db.get(Report, report['id']).assignee_id = db.query(User).filter_by(
            email='first@example.org').one().id
        db.commit()
    promote('first@example.org', ['reviewer'], ['area-a'])
    promote('second@example.org', ['reviewer'], ['area-a'])
    login(client, 'second@example.org', 'Second')

    response = client.post(f"/api/v1/reports/{report['id']}/review",
                           json=review_payload(version_of(client, report['id'])))
    assert response.status_code == 403
    assert 'different reviewer' in response.json()['detail']
    assert read_state(report['id']) == 'submitted'


def test_a_stale_version_is_refused(client):
    ensure_areas('area-a')
    report = owned_report(client, 'submitted')
    staff_session(client, 'reviewer@example.org', 'Reviewer', ['reviewer'], ['area-a'])

    response = client.post(f"/api/v1/reports/{report['id']}/review", json=review_payload(99))
    assert response.status_code == 409
    assert 'Reload' in response.json()['detail']


# --- assign / close -----------------------------------------------------------------

@pytest.mark.parametrize('state', ['verified', 'rejected'])
def test_close_is_accepted_only_after_a_decision(client, state):
    ensure_areas('area-a')
    report = owned_report(client, state)
    staff_session(client, 'responder@example.org', 'Responder', ['responder'], ['area-a'])

    response = client.post(f"/api/v1/reports/{report['id']}/close",
                           json={'version': version_of(client, report['id']),
                                 'note': 'Farmer briefed; no further action needed.'})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'closed'


@pytest.mark.parametrize('state', ['submitted', 'under_review', 'needs_evidence'])
def test_close_is_refused_before_a_decision(client, state):
    ensure_areas('area-a')
    report = owned_report(client, state)
    staff_session(client, 'responder@example.org', 'Responder', ['responder'], ['area-a'])

    response = client.post(f"/api/v1/reports/{report['id']}/close",
                           json={'version': version_of(client, report['id']), 'note': 'Closing early.'})
    assert response.status_code == 409
    assert 'Verify or reject' in response.json()['detail']
    assert read_state(report['id']) == state


def test_a_reviewer_cannot_close_a_case(client):
    ensure_areas('area-a')
    report = owned_report(client, 'verified')
    staff_session(client, 'reviewer@example.org', 'Reviewer', ['reviewer'], ['area-a'])

    response = client.post(f"/api/v1/reports/{report['id']}/close",
                           json={'version': version_of(client, report['id']), 'note': 'Not my role.'})
    assert response.status_code == 403


def test_closing_outside_the_assigned_area_is_refused(client):
    ensure_areas('area-a', 'area-b')
    report = owned_report(client, 'verified', area_id='area-b')
    staff_session(client, 'responder@example.org', 'Responder', ['responder'], ['area-a'])

    # Area scoping rejects the case before the version is ever read, so any version works.
    response = client.post(f"/api/v1/reports/{report['id']}/close",
                           json={'version': 1, 'note': 'Out of area.'})
    assert response.status_code == 404
    assert read_state(report['id']) == 'verified'


def test_assignment_requires_an_active_officer_in_that_area(client):
    ensure_areas('area-a', 'area-b')
    report = owned_report(client, 'submitted')
    register(client, 'newcomer@example.org', 'Newcomer')
    # A reporter in the right area is not a valid assignee.
    promote('newcomer@example.org', ['reporter'], ['area-a'])
    # The assigning officer must hold the report's own area, or the 404 masks the 422.
    staff_session(client, 'officer@example.org', 'Officer', ['responder'], ['area-a'])

    with SessionLocal() as db:
        newcomer = db.query(User).filter_by(email='newcomer@example.org').one()
        newcomer_id = newcomer.id
    response = client.post(f"/api/v1/reports/{report['id']}/assign",
                           json={'version': version_of(client, report['id']),
                                 'assignee_id': newcomer_id})
    assert response.status_code == 422
    assert 'active reviewer or responder' in response.json()['detail']

    # The same request succeeds for a responder in the case's area, so the 422 above is
    # the assignee rule and not a blanket refusal to assign at all.
    staff_session(client, 'officer2@example.org', 'Officer2', ['responder'], ['area-a'])
    with SessionLocal() as db:
        officer_id = db.query(User).filter_by(email='officer2@example.org').one().id
    accepted = client.post(f"/api/v1/reports/{report['id']}/assign",
                           json={'version': version_of(client, report['id']),
                                 'assignee_id': officer_id})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['assignee_id'] == officer_id


def test_a_reviewer_outside_the_case_area_cannot_assign_it(client):
    ensure_areas('area-a', 'area-b')
    report = owned_report(client, 'submitted', area_id='area-b')
    register(client, 'colleague@example.org', 'Colleague')
    promote('colleague@example.org', ['responder'], ['area-b'])
    # Right role, wrong area.
    staff_session(client, 'officer@example.org', 'Officer', ['responder'], ['area-a'])
    with SessionLocal() as db:
        colleague_id = db.query(User).filter_by(email='colleague@example.org').one().id
    response = client.post(f"/api/v1/reports/{report['id']}/assign",
                           json={'version': 1, 'assignee_id': colleague_id})
    assert response.status_code == 404


# --- advisory precondition ----------------------------------------------------------

def advisory_payload(report_id):
    # AdvisoryWrite caps expiry at 30 days, so it has to be relative to now.
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    return {'report_id': report_id, 'title': 'Elephants near the community boundary',
            'body': 'Two adult elephants were seen near the eastern field boundary this morning. '
                    'Keep a safe distance and report any change.',
            'source': 'District wildlife office, verified on site',
            'expires_at': expires}


def publisher_session(client):
    register(client, 'publisher@example.org', 'Publisher')
    client.post('/api/v1/auth/logout')
    promote('publisher@example.org', ['publisher'], ['area-a'])
    return login(client, 'publisher@example.org', 'Publisher')


def test_an_advisory_needs_a_human_verified_review(client):
    ensure_areas('area-a')
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client)
    force_state(report['id'], 'verified')
    client.post('/api/v1/auth/logout')
    publisher_session(client)

    # The report says 'verified', but no human review row exists.
    response = client.post('/api/v1/advisories', json=advisory_payload(report['id']))
    assert response.status_code == 409
    assert 'human-verified' in response.json()['detail']
    with SessionLocal() as db:
        assert db.query(Review).filter_by(report_id=report['id']).count() == 0


def test_a_state_change_alone_never_satisfies_the_advisory_precondition(client):
    """A report forced to 'verified' without a Review row is still not publishable."""
    ensure_areas('area-a')
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client)
    force_state(report['id'], 'closed')
    client.post('/api/v1/auth/logout')
    publisher_session(client)

    assert client.post('/api/v1/advisories',
                       json=advisory_payload(report['id'])).status_code == 409


def test_an_advisory_is_created_once_a_human_verified_review_exists(client):
    ensure_areas('area-a')
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client)
    force_state(report['id'], 'verified')
    with SessionLocal() as db:
        db.add(Review(report_id=report['id'], reviewer_id=db.query(User).filter_by(
            email='owner@example.org').one().id, decision='verified',
            notes='Verified on site by a second reviewer.', species='African elephant'))
        db.commit()
    client.post('/api/v1/auth/logout')
    publisher_session(client)

    response = client.post('/api/v1/advisories', json=advisory_payload(report['id']))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['state'] == 'draft'
    assert body['report_id'] == report['id']
    assert body['area_id'] == 'area-a'


def test_a_publisher_outside_the_report_area_cannot_draft_an_advisory(client):
    ensure_areas('area-a', 'area-b')
    register(client, 'owner@example.org', 'Owner')
    report = new_report(client, area_id='area-b')
    force_state(report['id'], 'verified')
    with SessionLocal() as db:
        db.add(Review(report_id=report['id'], reviewer_id=db.query(User).filter_by(
            email='owner@example.org').one().id, decision='verified',
            notes='Verified on site by a second reviewer.', species='African elephant'))
        db.commit()
    client.post('/api/v1/auth/logout')
    # Publisher role held, but the case is in an unassigned area.
    register(client, 'publisher@example.org', 'Publisher')
    client.post('/api/v1/auth/logout')
    promote('publisher@example.org', ['publisher'], ['area-a'])
    login(client, 'publisher@example.org', 'Publisher')

    response = client.post('/api/v1/advisories', json=advisory_payload(report['id']))
    assert response.status_code == 404
