"""T10a synthetic integration only: PostgreSQL, Floci and fixture Stripe.

This is not a phone, live Meta evaluation or Stripe test-mode receipt.
"""
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.adapters.stripe import FixtureStripe
from app.contracts.enums import SessionState
from app.jobs.queue import JobQueue
from app.main import create_app
from app.models import Artifact, Claim, Finding, Observation, Purchase, ReportRevision, Session, Upload
from app.pipeline.purge import PurgeHandler
from app.services.snapshots import build_paid_report
from tests.test_billing import (Env, WEBHOOK_SECRET, charge_object, deliver, drain, session_object,
                               start_checkout)
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate  # noqa: F401
from tests.test_report_routes import protected, ready, report_of  # noqa: F401
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


def test_synthetic_release_report_payment_refund_and_verified_purge(ready):
    h = ready.h
    report = report_of(ready)
    gateway = FixtureStripe()
    settings = h.settings.model_copy(update={"stripe_webhook_secret": SecretStr(WEBHOOK_SECRET),
                                              "public_sharing_enabled": False})
    client = TestClient(create_app(settings, database_probe=lambda _: True, session_factory=h.db,
                                  storage=h.storage, payments=gateway))
    env = Env(h.db, gateway, client, h.headers["Authorization"].split()[1], report.share_slug,
              report.id, h.session_id)
    path = f"/api/v1/reports/{report.share_slug}"
    html_path = f"/r/{report.share_slug}"
    paid_snapshot = build_paid_report(ready.snapshot())
    locked_ids = {str(f.id) for f in paid_snapshot.findings if f.zone.value != "driver_side"}
    assert locked_ids, "Fixture must exercise paid Part 2 evidence"

    def private_free():
        for endpoint in (path, html_path):
            anonymous = client.get(endpoint)
            assert anonymous.status_code == 404
            protected(anonymous)
            response = client.get(endpoint, headers=h.headers)
            assert response.status_code == 200
            protected(response)
            assert all(finding_id not in response.text for finding_id in locked_ids)
            assert "frame_key" not in response.text and "X-Amz" not in response.text
        response = client.get(path, headers=h.headers)
        assert response.json()["scope"] == "free"
        return response.json()

    free = private_free()
    purchase_id, checkout_id = start_checkout(env)
    # Provider simulation alone and browser navigation grant no entitlement.
    completed = gateway.simulate_completion(checkout_id)
    assert private_free() == free
    assert client.get(path + "?success=true&scope=paid", headers=h.headers).json() == free
    deliver(env, "checkout.session.completed", session_object(checkout_id, env))
    drain(env)
    paid = client.get(path, headers=h.headers)
    assert paid.status_code == 200 and paid.json()["scope"] == "paid"
    assert locked_ids <= {str(f["id"]) for f in paid.json()["findings"]}
    assert client.get(path).status_code == 404
    progress = client.get(f"/api/v1/sessions/{h.session_id}/progress", headers=h.headers)
    assert progress.json()["report"]["unlocked"] is True

    gateway.simulate_refund(checkout_id, 1900)
    deliver(env, "charge.refunded", charge_object(completed.payment_intent_id))
    drain(env)
    assert private_free() == free
    progress = client.get(f"/api/v1/sessions/{h.session_id}/progress", headers=h.headers)
    assert progress.json()["report"]["unlocked"] is False
    with h.db() as db:
        assert db.get(Purchase, purchase_id).state == "refunded"
        recorded_keys = tuple(db.scalars(select(Artifact.key).where(Artifact.session_id == h.session_id)))
    assert recorded_keys

    removed = client.post(f"/api/v1/sessions/{h.session_id}/delete-media", headers=h.headers, json={})
    assert removed.status_code == 202 and removed.json()["state"] == "deleting"
    for endpoint in (path, html_path, f"{path}/frames/{next(iter(locked_ids))}"):
        revoked = client.get(endpoint, headers=h.headers, follow_redirects=False)
        assert revoked.status_code == 410 and "location" not in revoked.headers
        protected(revoked)
    queue = JobQueue(h.db)
    lease = queue.claim("release-purge", ["purge"])
    assert lease is not None and lease.session_id == h.session_id
    publication = PurgeHandler(h.db, h.storage)(lease)
    assert queue.complete(lease, publication.publish)
    for namespace in ("raw", "media", "demo"):
        prefix = f"{namespace}/{h.session_id}/"
        assert h.storage.list_keys(prefix) == ()
        assert h.storage.list_multipart(prefix) == ()
    assert all(h.storage.head_size(key) is None for key in recorded_keys)
    with h.db() as db:
        assert db.get(Session, h.session_id).state == SessionState.DELETED
        for model in (Artifact, Claim, Finding, Observation, ReportRevision, Upload):
            assert not db.scalars(select(model).where(model.session_id == h.session_id)).all()
        assert db.get(Purchase, purchase_id).state == "refunded"
    assert client.get(path, headers=h.headers).status_code == 410
