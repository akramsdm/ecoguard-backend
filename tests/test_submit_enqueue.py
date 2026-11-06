import io
from PIL import Image

import pytest

from app.db import Base, engine, SessionLocal
from app.models import Area, Outbox, Prediction
from app.security import limiter
from tests.test_predictions_api import register, upload, ensure_area, image_bytes


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    limiter.memory.clear()
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
        __import__("app.main", fromlist=["app"]).app
    ) as tc:
        yield tc


@pytest.fixture(autouse=True)
def stub_runtime(monkeypatch):
    import sys, types, contextlib
    fake_torch = types.ModuleType("torch")
    fake_torch.inference_mode = lambda: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    from app.config import get_settings
    from app import ai
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    ai._classifier = None
    ai._load_error = None
    yield
    get_settings.cache_clear()
    ai.resolve_model_version.cache_clear()
    ai._classifier = None
    ai._load_error = None


def test_submit_enqueues_one_prediction_job(client):
    register(client)
    ensure_area()
    evidence = upload(client, image_bytes()).json()
    report = client.post(
        "/api/v1/reports",
        json={"client_id": "client-1", "area_id": "community-a", "category": "wildlife",
              "title": "title", "description": "description text here", "evidence_ids": [evidence["id"]],
              "observed_at": "2026-01-01T00:00:00Z", "consent": True},
    )
    assert report.status_code == 201, report.text
    rid = report.json()["id"]
    ver = report.json()["version"]
    r = client.post(f"/api/v1/reports/{rid}/submit", json={"version": ver})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        jobs = db.query(Outbox).filter_by(kind="prediction").count()
        preds = db.query(Prediction).filter_by(evidence_id=evidence["id"]).count()
    assert jobs == 1
    assert preds == 1


def _make_report(client, evidence_ids):
    ensure_area()
    report = client.post(
        "/api/v1/reports",
        json={"client_id": "client-1", "area_id": "community-a", "category": "wildlife",
              "title": "title", "description": "description text here", "evidence_ids": evidence_ids,
              "observed_at": "2026-01-01T00:00:00Z", "consent": True},
    )
    assert report.status_code == 201, report.text
    return report.json()


def test_submit_two_images_enqueues_two(client):
    register(client)
    ensure_area()
    e1 = upload(client, image_bytes()).json()
    e2 = upload(client, image_bytes()).json()
    report = _make_report(client, [e1["id"], e2["id"]])
    r = client.post(f"/api/v1/reports/{report['id']}/submit", json={"version": report["version"]})
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.query(Outbox).filter_by(kind="prediction").count() == 2
        assert db.query(Prediction).count() == 2


def test_submit_no_image(client):
    register(client)
    ensure_area()
    report = _make_report(client, [])
    r = client.post(f"/api/v1/reports/{report['id']}/submit", json={"version": report["version"]})
    assert r.status_code == 200
    assert r.json()["state"] == "submitted"
    with SessionLocal() as db:
        assert db.query(Outbox).filter_by(kind="prediction").count() == 0
        assert db.query(Prediction).count() == 0


def test_submit_twice_no_duplicates(client):
    register(client)
    ensure_area()
    evidence = upload(client, image_bytes()).json()
    report = _make_report(client, [evidence["id"]])
    r1 = client.post(f"/api/v1/reports/{report['id']}/submit", json={"version": report["version"]})
    assert r1.status_code == 200
    r2 = client.post(f"/api/v1/reports/{report['id']}/submit", json={"version": report["version"] + 1})
    # second submit is a safe retry; state already submitted
    assert r2.status_code in (200, 409)
    with SessionLocal() as db:
        assert db.query(Outbox).filter_by(kind="prediction").count() == 1
        assert db.query(Prediction).filter_by(evidence_id=evidence["id"]).count() == 1


def test_submit_enqueue_failure_does_not_block(client, monkeypatch):
    import app.evidence as ev

    def boom(db, evidence, user):
        raise RuntimeError("boom")

    monkeypatch.setattr(ev, "enqueue_prediction", boom)
    register(client)
    ensure_area()
    evidence = upload(client, image_bytes()).json()
    report = _make_report(client, [evidence["id"]])
    r = client.post(f"/api/v1/reports/{report['id']}/submit", json={"version": report["version"]})
    assert r.status_code == 200
    with SessionLocal() as db:
        from app.models import Report
        rr = db.get(Report, report["id"])
        assert rr.state == "submitted"