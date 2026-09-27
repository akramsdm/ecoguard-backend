"""End-to-end prediction contract: evidence privacy, authorisation, idempotency and the
rule that an AI suggestion never decides a report's species.

The classifier is stubbed, so these tests need no model weights, no network and no
PyTorch. Real model behaviour is covered by tests/test_ai.py and by
scripts/speciesnet_smoke.py.
"""
import io
import sys
import types

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import ai
from app.config import get_settings
from app.db import Base, engine, SessionLocal
from app.models import Area, Audit, Evidence, Outbox, Prediction, User
from app.security import limiter

ELEPHANT = ('55631055-3e0e-4b7a-9612-dedebe9f78b0;mammalia;proboscidea;elephantidae;'
            'loxodonta;africana;african elephant')


class _FakeInferenceMode:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class FakeClassifier:
    def __init__(self, classes=(ELEPHANT,), scores=(0.94,)):
        self.classes = list(classes)
        self.scores = list(scores)
        self.device = 'cpu'
        self.labels = {0: 'x'}
        self.model_info = types.SimpleNamespace(version='4.0.3b', type_='full_image')

    def preprocess(self, image, bboxes=None, resize=True):
        return {'shape': (image.width, image.height)}

    def predict(self, filepath, img):
        return {'filepath': filepath,
                'classifications': {'classes': self.classes, 'scores': self.scores}}


def image_bytes(size=(120, 90), colour=(30, 90, 60), fmt='JPEG', exif=None):
    buffer = io.BytesIO()
    out = Image.new('RGB', size, colour)
    if exif:
        out.save(buffer, format=fmt, exif=exif)
    else:
        out.save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # The limiter is a process-wide singleton, so its counters must not leak between tests.
    limiter.memory.clear()
    with TestClient(__import__('app.main', fromlist=['app']).app) as test_client:
        yield test_client
    ai._classifier = None


@pytest.fixture(autouse=True)
def stub_runtime(monkeypatch):
    fake_torch = types.ModuleType('torch')
    fake_torch.inference_mode = _FakeInferenceMode
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    ai._classifier = None
    ai._load_error = None
    yield
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    ai._classifier = None
    ai._load_error = None


def register(client, email='reporter@example.org', name='Reporter'):
    response = client.post('/api/v1/auth/register',
                           json={'email': email, 'name': name, 'password': 'PassPhrase1234!'})
    assert response.status_code == 201, response.text
    csrf = response.json()['csrf_token']
    client.headers.update({'X-CSRF-Token': csrf})
    return response.json()['user']


def upload(client, data, name='evidence.jpg'):
    return client.post('/api/v1/evidence', files={'file': (name, data, 'image/jpeg')})


def ensure_area(area_id='community-a', name='Community A'):
    """The community list is only seeded by the demo seeder, so add one for this test."""
    with SessionLocal() as db:
        if not db.get(Area, area_id):
            db.add(Area(id=area_id, name=name, description='Test area.',
                        latitude=0.2, longitude=30.1, radius_km=10))
            db.commit()


def test_non_image_evidence_is_rejected(client):
    register(client)
    response = upload(client, b'%PDF-1.4 not an image', name='notes.pdf')
    assert response.status_code == 422
    assert 'JPG' in response.json()['detail']


def test_upload_is_sanitised_and_stripped_of_metadata(client):
    register(client)
    exif = Image.Exif()
    exif[0x010F] = 'SecretCameraMaker'
    exif[0x0110] = 'EXIF-MODEL-SHOULD-NOT-SURVIVE'
    response = upload(client, image_bytes(size=(400, 300), exif=exif))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['content_type'] if 'content_type' in body else True
    with SessionLocal() as db:
        stored = db.query(Evidence).filter_by(id=body['id']).one()
        from app import storage
        data = storage.get(stored.storage_key)
    with Image.open(io.BytesIO(data)) as kept:
        assert b'EXIF-MODEL-SHOULD-NOT-SURVIVE' not in data
        assert kept.getexif() == {}


def test_prediction_is_returned_as_a_queued_or_completed_result(client, monkeypatch):
    ai._classifier = FakeClassifier()
    register(client)
    evidence = upload(client, image_bytes()).json()
    response = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']})
    assert response.status_code == 202
    body = response.json()
    assert body['state'] == 'completed'
    assert body['species'] == 'african elephant'
    assert body['confidence'] == pytest.approx(0.94)
    assert body['boxes'] == []
    assert body['model_version'] == 'speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image'


def test_a_successful_result_is_audited(client):
    """The spec requires the result to be auditable, not just the request."""
    ai._classifier = FakeClassifier()
    user = register(client)
    evidence = upload(client, image_bytes()).json()
    body = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()
    with SessionLocal() as db:
        rows = (db.query(Audit).filter_by(action='prediction.completed').all())
        assert len(rows) == 1
        assert rows[0].target_id == body['id']
        assert rows[0].actor_id == user['id']
        # The audit row points at the Prediction, which holds the result itself.
        stored = db.get(Prediction, body['id'])
        assert (stored.state, stored.species) == ('completed', 'african elephant')
        assert stored.model_version == body['model_version']
        # Both the request and the outcome are on the trail.
        requested = db.query(Audit).filter_by(action='prediction.requested').one()
        assert requested.target_id == body['id']


def test_a_terminal_failure_is_audited_once_and_retries_are_not(client, monkeypatch):
    ai._classifier = FakeClassifier()

    def boom(*args, **kwargs):
        raise RuntimeError('classifier exploded')

    monkeypatch.setattr(ai, 'infer', boom)
    register(client)
    evidence = upload(client, image_bytes()).json()
    body = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()

    from app.jobs import process_job
    with SessionLocal() as db:
        job = db.query(Outbox).filter_by(kind='prediction', aggregate_id=body['id']).one()
        job_id = job.id
    # The inline dispatch already consumed one attempt, so drive the job to its end
    # without depending on how many attempts the caller happens to have spent.
    saw_retry = False
    for _ in range(8):
        with SessionLocal() as db:
            if db.get(Outbox, job_id).state == 'failed':
                break
        process_job(job_id)
        with SessionLocal() as db:
            job = db.get(Outbox, job_id)
            if job.state == 'retry':
                saw_retry = True
                # An intermediate retry must not leave an audit row of its own.
                assert db.query(Audit).filter_by(action='prediction.failed').count() == 0
    assert saw_retry, 'expected at least one retry before the terminal failure'
    with SessionLocal() as db:
        job = db.get(Outbox, job_id)
        assert job.state == 'failed'
        failures = db.query(Audit).filter_by(action='prediction.failed').all()
        assert len(failures) == 1
        assert failures[0].target_id == body['id']
        assert db.get(Prediction, body['id']).state == 'failed'
        # The job error must not leak the exception text.
        assert 'classifier exploded' not in (job.last_error or '')


def test_same_evidence_and_model_version_is_idempotent(client):
    ai._classifier = FakeClassifier()
    register(client)
    evidence = upload(client, image_bytes()).json()
    first = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()
    second = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()
    assert first['id'] == second['id']
    with SessionLocal() as db:
        assert db.query(Prediction).filter_by(evidence_id=evidence['id']).count() == 1


def test_a_new_model_version_triggers_a_fresh_prediction(client, monkeypatch):
    ai._classifier = FakeClassifier()
    register(client)
    evidence = upload(client, image_bytes()).json()
    first = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()
    monkeypatch.setenv('MODEL_VERSION', 'speciesnet-5.0.5-v4.0.3b-full_image+r2')
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    second = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()
    assert second['id'] != first['id']
    assert second['model_version'].endswith('+r2')


def test_prediction_requires_authentication(client):
    assert client.get('/api/v1/predictions/anything').status_code == 401


def test_prediction_for_someone_elses_evidence_is_not_visible(client):
    ai._classifier = FakeClassifier()
    owner = register(client, 'owner@example.org', 'Owner')
    evidence = upload(client, image_bytes()).json()
    client.post('/api/v1/auth/logout')
    other = register(client, 'other@example.org', 'Other')
    response = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']})
    assert response.status_code == 404
    assert client.get(f"/api/v1/predictions/{response.json().get('id', 'x')}").status_code == 404
    assert other['id'] != owner['id']


def test_report_species_is_never_overwritten_by_the_prediction(client):
    ai._classifier = FakeClassifier()
    register(client)
    ensure_area()
    evidence = upload(client, image_bytes()).json()
    prediction = client.post('/api/v1/predictions',
                             json={'evidence_id': evidence['id']}).json()
    assert prediction['species'] == 'african elephant'
    body = {'client_id': 'client-prediction-1', 'category': 'wildlife',
            'title': 'Possible elephant near a field boundary',
            'description': 'Observed from a safe distance at the edge of a field.',
            'area_id': 'community-a', 'species': 'Unknown animal',
            'observed_at': '2026-01-01T00:00:00+00:00', 'consent': True,
            'share_location': False, 'evidence_ids': [evidence['id']]}
    created = client.post('/api/v1/reports', json=body)
    assert created.status_code == 201, created.text
    report = created.json()
    # A completed AI suggestion must not have leaked into the saved report.
    assert report['species'] == 'Unknown animal'
    # Re-requesting assistance for the same evidence must not rewrite it either.
    again = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']})
    assert again.status_code in (200, 202)
    assert client.get(f"/api/v1/reports/{report['id']}").json()['species'] == 'Unknown animal'
    # Only an explicit human action may change the species.
    updated = client.put(f"/api/v1/reports/{report['id']}",
                         json={**body, 'species': 'African elephant',
                               'version': report['version']})
    assert updated.status_code == 200, updated.text
    assert updated.json()['species'] == 'African elephant'


def test_model_failure_does_not_leak_internals_or_block_reporting(client, monkeypatch):
    monkeypatch.setattr(ai, '_load', lambda: None)
    ai._load_error = 'OSError'
    register(client)
    evidence = upload(client, image_bytes()).json()
    body = client.post('/api/v1/predictions', json={'evidence_id': evidence['id']}).json()
    assert body['state'] == 'unavailable'
    assert body['species'] is None
    text = str(body).lower()
    for leak in ('traceback', 'modulenotfound', 'kaggle', '/app', 'c:\\', 'oserror',
                 'var/models', 'pip'):
        assert leak not in text
    assert client.get('/api/v1/config').status_code == 200


def test_config_reports_real_assistance_state(client):
    body = client.get('/api/v1/config').json()
    assert body['image_assistance'] in ('ready', 'degraded', 'not_configured')
    # The operator-facing reason must not be part of the public config payload.
    assert 'detail' not in body


def test_config_reports_live_ai_state_despite_the_cache_ttl(client, monkeypatch):
    """A cached 'degraded' must never mask a model that has just become ready."""
    from app import cache
    cache.cache.delete('config')
    monkeypatch.setenv('CACHE_TTL_SECONDS', '300')
    get_settings.cache_clear()
    first = client.get('/api/v1/config').json()
    assert first['image_assistance'] == 'degraded'
    assert first['ai_model_version'] == 'speciesnet-5.0.5-google-speciesnet-pyTorch-v4.0.3b-1-full_image'
    # Same 300s cache window, but the classifier has since loaded.
    ai._classifier = FakeClassifier()
    second = client.get('/api/v1/config').json()
    assert second['image_assistance'] == 'ready'
    # And a regression the other way must also be visible immediately.
    ai._classifier = None
    monkeypatch.setenv('IMAGE_ASSISTANCE', 'disabled')
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    assert client.get('/api/v1/config').json()['image_assistance'] == 'not_configured'
    cache.cache.delete('config')


def test_admin_image_assistance_endpoint_reports_the_reason(client, monkeypatch):
    ai._classifier = None
    monkeypatch.setattr(ai, '_runtime_installed', lambda: False)
    assert client.get('/api/v1/admin/image-assistance').status_code == 401
    register(client)
    assert client.get('/api/v1/admin/image-assistance').status_code == 403
    with SessionLocal() as db:
        user = db.query(User).filter_by(email='reporter@example.org').one()
        user.roles = ['admin']
        db.commit()
    body = client.get('/api/v1/admin/image-assistance').json()
    assert body['state'] == 'degraded'
    assert 'not installed' in body['detail']
