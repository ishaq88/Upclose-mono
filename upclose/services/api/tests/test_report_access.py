"""Report authority, entitlement and locked-evidence regression checks."""
from datetime import datetime, timezone
from html import escape
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.main import create_app
from app.models import Entitlement, Purchase, Report, Session
from app.services.snapshots import build_paid_report
from app.settings import Settings
from fastapi.testclient import TestClient
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate  # noqa: F401
from tests.test_report_routes import get, protected, ready, report_of  # noqa: F401
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


def grant(rig, *, state="paid", revoked=False):
    h = rig.h
    with h.db.begin() as db:
        purchase = Purchase(session_id=h.session_id, report_id=report_of(rig).id, state=state,
                            checkout_id=f"cs_synthetic_{uuid4().hex}", payment_intent_id=f"pi_synthetic_{uuid4().hex}")
        db.add(purchase)
        db.flush()
        entitlement = Entitlement(session_id=h.session_id, report_id=purchase.report_id, source_purchase_id=purchase.id,
                                  revoked_at=datetime.now(timezone.utc) if revoked else None)
        db.add(entitlement)
        db.flush()
        return purchase.id, entitlement.id


def public_client(rig, enabled):
    settings = rig.h.settings.model_copy(update={"public_sharing_enabled": enabled})
    return TestClient(create_app(settings, database_probe=lambda _: True, session_factory=rig.h.db,
                                 storage=rig.h.storage))


def test_default_private_owner_authority_and_two_public_sharing_gates(ready):
    assert Settings().public_sharing_enabled is False
    h = ready.h
    slug = report_of(ready).share_slug
    foreign = h.other_owner()
    for global_enabled, report_enabled, expected in ((False, False, 404), (False, True, 404),
                                                    (True, False, 404), (True, True, 200)):
        with h.db.begin() as db:
            db.execute(update(Report).where(Report.id == report_of(ready).id).values(share_enabled=report_enabled))
        client = public_client(ready, global_enabled)
        for prefix in ("/api/v1/reports/", "/r/"):
            path = prefix + slug
            response = client.get(path)
            assert response.status_code == expected, response.text
            protected(response)
            assert client.get(path, headers=h.headers).status_code == 200
            assert client.get(path, headers=foreign).status_code == 404
            for auth in ("Bearer invalid", "Basic invalid", "Bearer"):
                denied = client.get(path, headers={"Authorization": auth})
                assert denied.status_code == 401, denied.text
                protected(denied)
    missing = h.http.get("/api/v1/reports/" + "x" * 32, headers=h.headers)
    assert missing.status_code == 404
    protected(missing)


def test_free_html_json_progress_and_media_hide_all_locked_evidence(ready):
    h = ready.h
    paid = build_paid_report(ready.snapshot())
    locked = [f for f in paid.findings if f.zone.value != "driver_side"]
    assert locked and paid.claims and paid.repairs.items
    responses = [get(ready, path, headers=h.headers) for path in
                 ("/api/v1/reports/{slug}", "/r/{slug}")]
    progress = h.http.get(f"/api/v1/sessions/{h.session_id}/progress", headers=h.headers)
    assert progress.status_code == 200 and progress.json()["report"]["unlocked"] is False
    free = responses[0].json()
    assert not {"claims", "narration", "repairs", "scores"} & free.keys()
    assert all(f["zone"] == "driver_side" for f in free["findings"])
    assert {z["zone"] for z in free["locked_zones"]} >= {"passenger_side"}
    for response in (*responses, progress):
        assert response.status_code == 200
        for finding in locked:
            assert str(finding.id) not in response.text
        for claim in paid.claims:
            assert claim.text not in response.text and claim.rationale not in response.text
        assert "No rust anywhere." not in response.text
        assert "repair_total" not in response.text and "total_low_cents" not in response.text
        assert "frame_key" not in response.text and "X-Amz" not in response.text
    for finding in locked:
        response = get(ready, f"/api/v1/reports/{{slug}}/frames/{finding.id}", headers=h.headers,
                       follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers
    for query in ("?paid=true", "?scope=paid", "?success=true&session_id=synthetic", "?unlocked=1"):
        response = get(ready, "/api/v1/reports/{slug}" + query, headers=h.headers)
        assert response.json() == free


def test_only_active_paid_entitlement_unlocks_and_refund_preserves_other_purchase(ready):
    h = ready.h
    purchase_id, entitlement_id = grant(ready, state="pending")
    def scope():
        response = get(ready, "/api/v1/reports/{slug}", headers=h.headers)
        assert response.status_code == 200, response.text
        progress = h.http.get(f"/api/v1/sessions/{h.session_id}/progress", headers=h.headers).json()
        assert progress["report"]["unlocked"] == (response.json()["scope"] == "paid")
        if progress["free_verdict"]:
            assert all(f["zone"] == "driver_side" for f in progress["free_verdict"]["findings"])
        return response.json()
    assert scope()["scope"] == "free"
    with h.db.begin() as db:
        db.execute(update(Purchase).where(Purchase.id == purchase_id).values(state="paid"))
    paid = scope()
    assert paid["scope"] == "paid" and paid["claims"] and paid["repairs"]
    assert any(f["zone"] == "passenger_side" for f in paid["findings"])
    assert all(f["evidence_available"] is False for f in paid["findings"])
    html = get(ready, "/r/{slug}", headers=h.headers)
    assert html.status_code == 200
    assert all(escape(claim["text"]) in html.text for claim in paid["claims"])
    assert "Passenger side" in html.text and "<script" not in html.text.lower()
    protected(html)
    for finding in paid["findings"]:
        frame = get(ready, f"/api/v1/reports/{{slug}}/frames/{finding['id']}", headers=h.headers,
                    follow_redirects=False)
        assert frame.status_code == 404 and "location" not in frame.headers
    with h.db.begin() as db:
        db.execute(update(Entitlement).where(Entitlement.id == entitlement_id).values(revoked_at=datetime.now(timezone.utc)))
    assert scope()["scope"] == "free"
    with h.db.begin() as db:
        db.execute(update(Entitlement).where(Entitlement.id == entitlement_id).values(revoked_at=None))
        db.execute(update(Purchase).where(Purchase.id == purchase_id).values(state="refunded"))
    assert scope()["scope"] == "free"
    second_purchase, _ = grant(ready)
    assert second_purchase != purchase_id and scope()["scope"] == "paid"
    with h.db.begin() as db:
        db.execute(update(Report).where(Report.id == report_of(ready).id).values(share_enabled=True))
    public = public_client(ready, True).get("/api/v1/reports/" + report_of(ready).share_slug)
    assert public.status_code == 200 and public.json()["scope"] == "paid"
    protected(public)


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_removed_reports_revoke_json_html_and_frames(ready, state):
    h = ready.h
    finding = ready.snapshot().manifest.all_findings[0]
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(state=state,
                   deleted_at=datetime.now(timezone.utc)))
        db.execute(update(Report).where(Report.id == report_of(ready).id).values(share_enabled=True))
    client = public_client(ready, True)
    for path in ("/api/v1/reports/{slug}", "/r/{slug}", f"/api/v1/reports/{{slug}}/frames/{finding.id}"):
        response = client.get(path.format(slug=report_of(ready).share_slug), follow_redirects=False)
        assert response.status_code == 410 and "location" not in response.headers
        assert str(finding.id) not in response.text
        protected(response)
