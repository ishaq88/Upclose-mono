"""Share-with-mechanic toggle: owner-only enable/disable behind paid + ready gates.

PostgreSQL/API tests only (disposable ``upclose_test_*`` databases via
``.tools/verify-review.ps1``). Public reads reuse the existing report path:
the global ``public_sharing_enabled`` gate and the approved-derivative-only
evidence rule are unchanged by the toggle.
"""
from uuid import uuid4

from sqlalchemy import select, update

from app.contracts.evidence import BoundingBox
from app.models import Entitlement, Purchase, Report, ReportRevision, Session
from app.services.accounts import AccountService
from app.services.privacy import RECEIPT, PrivacyService
from app.settings import Settings
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate  # noqa: F401
from tests.test_report_access import grant, public_client
from tests.test_report_routes import protected, ready, report_of  # noqa: F401
from tests.test_uploads import bucket, make, session_factory  # noqa: F401

PASSWORD = "correct-horse-10"


def share_path(rig) -> str:
    return f"/api/v1/reports/{report_of(rig).share_slug}/share"


def share_enabled(rig) -> bool:
    with rig.h.db() as db:
        return db.scalar(select(Report.share_enabled).where(Report.id == report_of(rig).id))


def problem(response, status: int, code: str):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code
    protected(response)
    return response.json()


def test_owner_can_enable_and_disable_idempotently(ready):
    h = ready.h
    grant(ready)
    assert share_enabled(ready) is False
    enabled = h.http.post(share_path(ready), headers=h.headers)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json() == {"url": f"/r/{report_of(ready).share_slug}",
                              "share_enabled": True, "public_access_active": False}
    assert share_enabled(ready) is True
    again = h.http.post(share_path(ready), headers=h.headers)
    assert again.status_code == 200 and again.json() == enabled.json()
    disabled = h.http.delete(share_path(ready), headers=h.headers)
    assert disabled.status_code == 200 and disabled.json() == {"share_enabled": False}
    assert share_enabled(ready) is False
    assert h.http.delete(share_path(ready), headers=h.headers).json() == {"share_enabled": False}


def test_share_requires_owner_authority(ready):
    h = ready.h
    grant(ready)
    foreign = h.other_owner()
    for method in ("post", "delete"):
        call = getattr(h.http, method)
        problem(call(share_path(ready), headers=foreign), 404, "NOT_FOUND")
        problem(call(share_path(ready)), 401, "TOKEN_INVALID")
        problem(call(share_path(ready), headers={"Authorization": "Bearer " + "x" * 43}), 401, "TOKEN_INVALID")
    problem(h.http.post("/api/v1/reports/" + "x" * 32 + "/share", headers=h.headers), 404, "NOT_FOUND")
    assert share_enabled(ready) is False


def test_account_token_alone_cannot_share(ready):
    h = ready.h
    grant(ready)
    service = AccountService(h.db, Settings(account_pbkdf2_iterations=1000))
    account = service.register(f"dealer-{uuid4().hex[:16]}@example.com", PASSWORD)
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(account_id=account.id))
    account_headers = {"Authorization": "Bearer " + service.issue_token(account.id).token}
    # The account token reads its linked report, but it is not owner authority.
    assert h.http.get(f"/api/v1/reports/{report_of(ready).share_slug}", headers=account_headers).status_code == 200
    problem(h.http.post(share_path(ready), headers=account_headers), 401, "TOKEN_INVALID")
    problem(h.http.delete(share_path(ready), headers=account_headers), 401, "TOKEN_INVALID")
    assert share_enabled(ready) is False


def test_unpaid_revoked_and_refunded_reports_cannot_share(ready):
    h = ready.h
    problem(h.http.post(share_path(ready), headers=h.headers), 403, "SHARE_REQUIRES_PAID")
    assert share_enabled(ready) is False
    purchase_id, entitlement_id = grant(ready, revoked=True)
    problem(h.http.post(share_path(ready), headers=h.headers), 403, "SHARE_REQUIRES_PAID")
    with h.db.begin() as db:
        db.execute(update(Entitlement).where(Entitlement.id == entitlement_id).values(revoked_at=None))
    assert h.http.post(share_path(ready), headers=h.headers).status_code == 200
    assert h.http.delete(share_path(ready), headers=h.headers).status_code == 200
    with h.db.begin() as db:
        db.execute(update(Purchase).where(Purchase.id == purchase_id).values(state="refunded"))
    problem(h.http.post(share_path(ready), headers=h.headers), 403, "SHARE_REQUIRES_PAID")
    assert share_enabled(ready) is False
    # Disable remains available after a refund so access can always be revoked.
    assert h.http.delete(share_path(ready), headers=h.headers).json() == {"share_enabled": False}


def test_not_ready_report_cannot_share_but_can_disable(ready):
    h = ready.h
    grant(ready)
    with h.db.begin() as db:
        revision = ReportRevision(session_id=h.session_id, report_id=report_of(ready).id, revision_no=2)
        db.add(revision)
        db.flush()
        db.execute(update(Report).where(Report.id == report_of(ready).id)
                   .values(current_revision_id=revision.id))
    problem(h.http.post(share_path(ready), headers=h.headers), 409, "REPORT_NOT_READY")
    assert share_enabled(ready) is False
    assert h.http.delete(share_path(ready), headers=h.headers).json() == {"share_enabled": False}


def test_global_gate_off_enable_reports_inactive_and_public_still_404(ready):
    h = ready.h
    grant(ready)
    enabled = h.http.post(share_path(ready), headers=h.headers)
    assert enabled.json()["public_access_active"] is False
    slug = report_of(ready).share_slug
    for path in (f"/api/v1/reports/{slug}", f"/r/{slug}"):
        anonymous = h.http.get(path)
        assert anonymous.status_code == 404
        protected(anonymous)
        assert h.http.get(path, headers=h.headers).status_code == 200


def test_global_gate_on_shared_link_serves_paid_and_disable_stops(ready):
    h = ready.h
    grant(ready)
    slug = report_of(ready).share_slug
    client = public_client(ready, True)
    for path in (f"/api/v1/reports/{slug}", f"/r/{slug}"):
        assert client.get(path).status_code == 404  # unshared link
    assert client.get("/api/v1/reports/" + "x" * 32).status_code == 404  # stale/unknown slug
    enabled = client.post(share_path(ready), headers=h.headers)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json() == {"url": f"/r/{slug}", "share_enabled": True, "public_access_active": True}
    paid = client.get(f"/api/v1/reports/{slug}")
    assert paid.status_code == 200 and paid.json()["scope"] == "paid"
    protected(paid)
    html = client.get(f"/r/{slug}")
    assert html.status_code == 200 and "Passenger side" in html.text
    protected(html)
    assert client.delete(share_path(ready), headers=h.headers).json() == {"share_enabled": False}
    for path in (f"/api/v1/reports/{slug}", f"/r/{slug}"):
        assert client.get(path).status_code == 404
        assert client.get(path, headers=h.headers).status_code == 200


def test_shared_views_expose_no_raw_or_unapproved_images(ready):
    h = ready.h
    grant(ready)
    assert h.http.post(share_path(ready), headers=h.headers).status_code == 200
    client = public_client(ready, True)
    slug = report_of(ready).share_slug
    snapshot = ready.snapshot()
    frame_keys = {finding.evidence.frame_key for finding in snapshot.manifest.all_findings}
    assert frame_keys
    html = client.get(f"/r/{slug}")
    assert html.status_code == 200
    assert "<img" not in html.text and "/frames/" not in html.text and "X-Amz" not in html.text
    for key in frame_keys:
        assert key not in html.text
    body = client.get(f"/api/v1/reports/{slug}").json()
    assert all(f["evidence_available"] is False for f in body["findings"])
    assert "frame_key" not in client.get(f"/api/v1/reports/{slug}").text
    for finding in snapshot.manifest.all_findings:
        frame = client.get(f"/api/v1/reports/{slug}/frames/{finding.id}", follow_redirects=False)
        assert frame.status_code == 404 and "location" not in frame.headers


def test_shared_public_view_serves_only_approved_derivative(ready):
    h = ready.h
    grant(ready)
    service = PrivacyService(h.db, h.storage)
    finding = next(f for f in ready.snapshot().manifest.all_findings
                   if f.part_no == 1 and f.status == "verified")
    created = service.create(h.session_id, ready.revision().id, finding.id,
                             (BoundingBox(x=0, y=0, w=.1, h=.1),))
    service.approve(created["id"], created["sha256"], RECEIPT)
    assert h.http.post(share_path(ready), headers=h.headers).status_code == 200
    client = public_client(ready, True)
    slug = report_of(ready).share_slug
    html = client.get(f"/r/{slug}")
    assert html.status_code == 200
    assert f"/api/v1/reports/{slug}/frames/{finding.id}" in html.text
    assert finding.evidence.frame_key not in html.text and "X-Amz" not in html.text
    for other in ready.snapshot().manifest.all_findings:
        if other.id != finding.id:
            assert f"/frames/{other.id}" not in html.text
            frame = client.get(f"/api/v1/reports/{slug}/frames/{other.id}", follow_redirects=False)
            assert frame.status_code == 404 and "location" not in frame.headers
    approved = client.get(f"/api/v1/reports/{slug}/frames/{finding.id}", follow_redirects=False)
    assert approved.status_code == 302 and "/derivatives/" in approved.headers["location"]
    assert "X-Amz-Expires=60" in approved.headers["location"]
