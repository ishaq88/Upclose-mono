"""T08a HTTP acceptance on real PostgreSQL and synthetic fixture media only."""
from __future__ import annotations

import re
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import Artifact, Report, ReportRevision, Session
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate, rows  # noqa: F401
from tests.test_report_render import pins_in
from tests.test_report_stages import build
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


@pytest.fixture
def ready(make, media_dir, scratch_root):
    return build(make, media_dir, scratch_root, parts=(1, 2), listing="No rust anywhere.")


def report_of(rig):
    return rows(rig.h, Report)[0]


def protected(response):
    assert "no-store" in response.headers.get("cache-control", "")
    assert "noindex" in response.headers.get("x-robots-tag", "")
    assert response.headers.get("referrer-policy") == "no-referrer"


def get(rig, path, **kwargs):
    return rig.h.http.get(path.format(slug=report_of(rig).share_slug), **kwargs)


def test_ready_json_and_no_javascript_html_share_scope_and_findings(ready):
    response = get(ready, "/api/v1/reports/{slug}", headers=ready.h.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "free" and body["state"] == "ready"
    assert body["revision_id"] == str(ready.revision().id)
    assert len(body["findings"]) > 0
    html = get(ready, "/r/{slug}", headers=ready.h.headers)
    assert html.status_code == 200 and "text/html" in html.headers["content-type"]
    assert "<script" not in html.text.lower() and "approximate" in html.text.lower()
    assert "synthetic" in html.text.lower()
    assert re.findall(r'data-view="(\w+)"', html.text) == ["top", "left", "right", "front", "rear"]
    cards = [int(n) for n in re.findall(r'id="finding-(\d+)"', html.text)]
    assert cards == list(range(1, len(body["findings"]) + 1))
    assert sorted(n for _, n in pins_in(html.text)) == cards
    assert "Evidence image withheld" in html.text and "<img" not in html.text
    assert "default-src 'none'" in html.headers["content-security-policy"]
    for result in (response, html):
        protected(result)
        assert result.headers["x-upclose-provenance"] == "synthetic"
        assert ready.h.headers["Authorization"].split()[1] not in result.text


@pytest.mark.parametrize("state,status", [("building", 202), ("failed", 200)])
def test_unfinished_revision_returns_only_minimal_status(make, state, status):
    h = make()
    finalized = h.http.post(f"/api/v1/sessions/{h.session_id}/finalize", headers=h.headers, json={})
    slug = finalized.json()["report_slug"]
    with h.db.begin() as db:
        report = db.scalar(select(Report).where(Report.session_id == h.session_id))
        db.execute(update(ReportRevision).where(ReportRevision.id == report.current_revision_id).values(state=state))
    for prefix in ("/api/v1/reports/", "/r/"):
        response = h.http.get(prefix + slug, headers=h.headers)
        assert response.status_code == status, response.text
        protected(response)
        if prefix.startswith("/api"):
            assert set(response.json()) == {"scope", "slug", "revision_id", "state"}
            assert response.json()["state"] == state
        else:
            assert "<script" not in response.text.lower()
        assert "manifest" not in response.text and "score_result" not in response.text


def test_current_revision_is_authoritative_and_missing_snapshot_fails_closed(ready):
    h = ready.h
    old = ready.revision()
    old_finding = ready.snapshot().manifest.all_findings[0].id
    with h.db.begin() as db:
        revision = ReportRevision(session_id=h.session_id, report_id=old.report_id, revision_no=2)
        db.add(revision)
        db.flush()
        db.execute(update(Report).where(Report.id == old.report_id).values(current_revision_id=revision.id))
    response = get(ready, "/api/v1/reports/{slug}", headers=h.headers)
    assert response.status_code == 202 and response.json()["revision_id"] != str(old.id)
    assert "findings" not in response.json()
    stale_frame = get(ready, f"/api/v1/reports/{{slug}}/frames/{old_finding}", headers=h.headers,
                      follow_redirects=False)
    assert stale_frame.status_code == 404 and "location" not in stale_frame.headers
    with h.db.begin() as db:
        db.execute(update(Report).where(Report.id == old.report_id).values(current_revision_id=None))
    response = get(ready, "/api/v1/reports/{slug}", headers=h.headers)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "REPORT_NOT_READY"
    protected(response)


def test_corrupt_ready_snapshot_is_sanitized(ready, monkeypatch):
    from app.services import reports
    from app.services.snapshots import SnapshotIntegrityError

    snapshot = ready.snapshot()
    for field in ("score", "free_narration", "paid_narration", "rendered_at"):
        monkeypatch.setattr(reports, "load_snapshot", lambda *a, **k: replace(snapshot, **{field: None}))
        response = get(ready, "/api/v1/reports/{slug}", headers=ready.h.headers)
        assert response.status_code == 503 and response.json()["code"] == "REPORT_NOT_READY"
        protected(response)

    def corrupt(*args, **kwargs):
        raise SnapshotIntegrityError("private/provider/payload-secret")

    monkeypatch.setattr(reports, "load_snapshot", corrupt)
    for path in ("/api/v1/reports/{slug}", "/r/{slug}"):
        response = get(ready, path, headers=ready.h.headers)
        assert response.status_code == 503 and response.json()["code"] == "REPORT_NOT_READY"
        assert "payload-secret" not in response.text
        protected(response)


def test_all_frames_withheld_including_approved_derivative(ready):
    h = ready.h
    snapshot = ready.snapshot()
    ids = [finding.id for finding in snapshot.manifest.all_findings]
    with h.db.begin() as db:
        db.add(Artifact(session_id=h.session_id, revision_id=ready.revision().id, kind="derivative",
                        key=f"synthetic/approved/{uuid4().hex}.jpg", sha256="a" * 64, privacy_state="approved"))
    for finding_id in (*ids, uuid4()):
        response = get(ready, f"/api/v1/reports/{{slug}}/frames/{finding_id}", headers=h.headers,
                       follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers
        protected(response)
    body = get(ready, "/api/v1/reports/{slug}", headers=h.headers).json()
    assert all(finding["evidence_available"] is False for finding in body["findings"])
