"""Vehicle label: owner-only write, account/My-reports + report header surfaces.

PostgreSQL tests follow test_sessions.py (isolated upclose_test_* only).
Pure contract/render tests run without a database.
"""
from __future__ import annotations

from decimal import Decimal
import ipaddress
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.contracts.api import UpdateVehicleLabelRequest
from app.contracts.enums import CoverageState, ReportState, Zone
from app.contracts.reports import FreeReport, PaidReport, PublicCoverage
from app.contracts.scoring import RepairSummary, ScoreResult, ZoneScore
from app.main import create_app
from app.models import Session
from app.report import render_report
from app.settings import Settings
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate  # noqa: F401
from tests.test_report_routes import ready, report_of  # noqa: F401
from tests.test_uploads import bucket, make  # noqa: F401

PASSWORD = "correct-horse-10"
LABEL = "2004 panel van, white"


# --- pure contract + render (no DB) -----------------------------------------

@pytest.mark.parametrize("raw,expected", [(LABEL, LABEL), ("  padded label  ", "padded label"), ("x" * 80, "x" * 80)])
def test_label_contract_trims_and_accepts_bounds(raw, expected):
    assert UpdateVehicleLabelRequest(label=raw).label == expected


@pytest.mark.parametrize("raw", ["", "   ", "x" * 81, "bad\x00label", "bad\x1flabel", "bad\x7flabel", "line\nbreak"])
def test_label_contract_rejects_empty_long_and_control(raw):
    with pytest.raises(Exception):
        UpdateVehicleLabelRequest(label=raw)


def _free(label):
    return FreeReport(slug="s", revision_id=uuid4(), state=ReportState.READY, vehicle_label=label,
                      coverage=(PublicCoverage(zone=Zone.DRIVER_SIDE, state=CoverageState.ASSESSED),),
                      part1_score=None, findings=(), locked_zones=(), part1_summary="summary")


def _paid(label):
    return PaidReport(slug="s", revision_id=uuid4(), state=ReportState.READY, vehicle_label=label,
                      coverage=(), scores=ScoreResult(headline=None, zones=(ZoneScore(zone=Zone.DRIVER_SIDE, state=CoverageState.ASSESSED, score=Decimal("80")),), config_hash="a" * 64),
                      findings=(), repairs=RepairSummary(items=(), subtotal_low_cents=0, subtotal_high_cents=0, unestimated_count=0, table_version="v1"),
                      claims=(), narration=())


def test_report_header_shows_label_free_and_paid_and_falls_back_when_null():
    for report in (_free(LABEL), _paid(LABEL)):
        html = render_report(report)
        assert LABEL in html and f"<title>UpClose visual condition report — {LABEL}</title>" in html
    for report in (_free(None), _paid(None)):
        html = render_report(report)
        assert "<title>UpClose visual condition report</title>" in html and 'class="vehicle-label"' not in html


def test_report_header_escapes_label():
    evil = '<script>alert(1)</script>'
    html = render_report(_free(evil))
    assert "<script" not in html.lower() and "&lt;script&gt;" in html


# --- PostgreSQL API -----------------------------------------------------------

@pytest.fixture(scope="module")
def session_factory():
    url = os.environ.get("SESSION_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("An isolated PostgreSQL SESSION_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing label tests outside an explicitly named upclose_test_* PostgreSQL database")
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def app(session_factory):
    return create_app(Settings(account_pbkdf2_iterations=1000), database_probe=lambda _: True, session_factory=session_factory)


def client(app):
    address = str(ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int & ((1 << 64) - 1))))
    return TestClient(app, client=(address, 12345))


def owner(c):
    r = c.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True, "consent_version": "upclose-prototype-v1"})
    assert r.status_code == 201, r.text
    d = r.json()
    return d["session_id"], {"Authorization": "Bearer " + d["session_token"]}, d["session_token"]


def register(c):
    r = c.post("/api/v1/accounts/register", json={"email": f"dealer-{uuid4().hex[:16]}@example.com", "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()


def test_set_get_label_trims_and_defaults_null(app, session_factory):
    c = client(app)
    sid, headers, _ = owner(c)
    assert c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["vehicle_label"] is None
    r = c.put(f"/api/v1/sessions/{sid}/label", headers=headers, json={"label": f"  {LABEL}  "})
    assert r.status_code == 200, r.text
    assert r.json()["vehicle_label"] == LABEL and r.json()["listing_version"] == 1
    assert c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["vehicle_label"] == LABEL
    with session_factory() as db:
        assert db.get(Session, sid).vehicle_label == LABEL


@pytest.mark.parametrize("raw_label", ["", "   ", "x" * 81, "a\x00b", "a\x1fb", "a\x7fb"])
def test_label_validation_rejected(app, raw_label):
    c = client(app)
    sid, headers, _ = owner(c)
    r = c.put(f"/api/v1/sessions/{sid}/label", headers=headers, json={"label": raw_label})
    assert r.status_code == 422, r.text
    assert c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["vehicle_label"] is None


def test_label_settable_after_finalize_without_listing_version_change(app):
    c = client(app)
    sid, headers, _ = owner(c)
    assert c.post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={}).status_code == 202
    r = c.put(f"/api/v1/sessions/{sid}/label", headers=headers, json={"label": LABEL})
    assert r.status_code == 200 and r.json()["state"] == "finalized" and r.json()["listing_version"] == 1


def test_label_owner_only_stranger_404_and_account_token_cannot_write(app):
    c = client(app)
    sid, headers, token = owner(c)
    _, stranger_headers, _ = owner(client(app))
    assert c.put(f"/api/v1/sessions/{sid}/label", headers=stranger_headers, json={"label": "x"}).status_code == 404
    assert c.put(f"/api/v1/sessions/{sid}/label", headers={"Authorization": "Bearer " + "x" * 43}, json={"label": "x"}).status_code == 401
    account = register(c)
    acct = {"Authorization": "Bearer " + account["token"]}
    assert c.post("/api/v1/accounts/claim", headers=acct, json={"session_id": sid, "owner_token": token}).status_code == 200
    # Account token alone: write rejected, existing read paths still work.
    assert c.put(f"/api/v1/sessions/{sid}/label", headers=acct, json={"label": "x"}).status_code == 401
    assert c.get(f"/api/v1/sessions/{sid}", headers=acct).status_code == 200
    mine = c.get("/api/v1/accounts/sessions", headers=acct)
    assert mine.status_code == 200 and mine.json()["sessions"][0]["vehicle_label"] is None
    c.put(f"/api/v1/sessions/{sid}/label", headers=headers, json={"label": LABEL})
    mine = c.get("/api/v1/accounts/sessions", headers=acct).json()
    assert mine["sessions"][0]["vehicle_label"] == LABEL
    assert c.get(f"/api/v1/sessions/{sid}", headers=acct).json()["vehicle_label"] == LABEL


def test_ready_report_json_and_html_carry_db_label(ready):
    h = ready.h
    put = h.http.put(f"/api/v1/sessions/{h.session_id}/label", headers=h.headers, json={"label": LABEL})
    assert put.status_code == 200, put.text
    slug = report_of(ready).share_slug
    body = h.http.get(f"/api/v1/reports/{slug}", headers=h.headers)
    assert body.status_code == 200 and body.json()["vehicle_label"] == LABEL
    html = h.http.get(f"/r/{slug}", headers=h.headers)
    assert html.status_code == 200 and LABEL in html.text


def test_migration_round_trip(session_factory):
    url = os.environ.get("SESSION_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    env = {**os.environ, "DATABASE_URL": url}

    def column_exists():
        with session_factory() as db:
            return db.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='sessions' AND column_name='vehicle_label'")) == 1

    assert column_exists()
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "c9549a675cf6"], check=True, env=env)
    try:
        assert not column_exists()
    finally:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    assert column_exists()
