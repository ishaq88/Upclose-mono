"""Adversarial checks for frozen report identities and persisted stage outputs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import Claim, ClaimEvidence, ReportRevision
from app.services import snapshots
from app.pipeline.orchestration import canonical_hash
from app.services.snapshots import SnapshotIntegrityError, compute_score, load_manifest, load_report_config, load_snapshot
from tests.test_report_stages import build
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_models import db, model_engine, rejects, row, seed  # noqa: F401
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


@pytest.mark.parametrize("identity", ["session_id", "report_id", "revision_id", "revision_no"])
def test_manifest_identity_cannot_be_rebound_even_with_a_matching_hash(make, media_dir, scratch_root, identity):
    rig = build(make, media_dir, scratch_root, listing="No rust.")
    revision = rig.revision()
    payload = deepcopy(revision.manifest)
    payload[identity] = 99 if identity == "revision_no" else str(uuid4())
    revision.manifest, revision.manifest_hash = payload, canonical_hash(payload)
    with pytest.raises((SnapshotIntegrityError, ValidationError)):
        load_manifest(revision)


@pytest.mark.parametrize("field", ["scoring_config_hash", "cost_config_hash", "numeric_score"])
def test_snapshot_rejects_a_forged_score_bound_to_the_correct_manifest(make, media_dir, scratch_root, field):
    rig = build(make, media_dir, scratch_root, listing="No rust.")
    revision = rig.revision()
    payload = deepcopy(revision.score_result)
    if field == "numeric_score":
        payload["scores"]["zones"][0]["score"] = 100
    else:
        payload[field] = "f" * 64
    revision.score_result = payload
    with rig.h.db() as db, pytest.raises(SnapshotIntegrityError):
        load_snapshot(db, revision)


def test_score_replay_rejects_config_drift_and_a_false_manifest_hash(make, media_dir, scratch_root):
    rig = build(make, media_dir, scratch_root, listing="No rust.")
    revision = rig.revision()
    manifest, config = load_manifest(revision), load_report_config()
    drifted = replace(config, scoring=config.scoring.model_copy(update={"version": "drifted"}))
    with pytest.raises(SnapshotIntegrityError):
        compute_score(manifest, revision.manifest_hash, drifted)
    with pytest.raises(SnapshotIntegrityError):
        compute_score(manifest, "f" * 64, config)


@pytest.mark.parametrize("field", ["config_hash", "statements_hash", "scope"])
def test_snapshot_rejects_free_narration_hash_or_scope_tampering(make, media_dir, scratch_root, field):
    rig = build(make, media_dir, scratch_root, listing="No rust.")
    revision = rig.revision()
    payload = deepcopy(revision.free_narration)
    payload["outcome"][field] = "paid" if field == "scope" else "f" * 64
    revision.free_narration = payload
    with rig.h.db() as db, pytest.raises(SnapshotIntegrityError):
        load_snapshot(db, revision)


@pytest.mark.parametrize("field", ["config_hash", "listing_hash", "claim_count"])
def test_snapshot_rejects_claim_metadata_tampering(make, media_dir, scratch_root, field):
    rig = build(make, media_dir, scratch_root, listing="No rust.")
    revision = rig.revision()
    payload = deepcopy(revision.paid_narration)
    payload["claims"][field] = 99 if field == "claim_count" else "f" * 64
    revision.paid_narration = payload
    with rig.h.db() as db, pytest.raises(SnapshotIntegrityError):
        load_snapshot(db, revision)


def test_claim_citation_requires_membership_in_this_revision_even_for_same_session(db):
    s = seed(db)
    other = row(db, ReportRevision, session_id=s["session"]["id"], report_id=s["report"]["id"], revision_no=2)
    # This finding belongs to the session and revision 1, but not revision 2.
    from app.models import Claim
    claim = row(db, Claim, session_id=s["session"]["id"], report_revision_id=other["id"], claim_index=0,
                text="No rust", verdict="contradicted", rationale="Requires this revision's evidence")
    rejects(db, insert(ClaimEvidence).values(claim_id=claim["id"], finding_id=s["finding"]["id"],
                                           report_revision_id=other["id"], session_id=s["session"]["id"],
                                           frame_key=s["frame"]["key"]))


def test_historical_snapshot_reads_use_frozen_configs_after_current_config_changes(make, media_dir, scratch_root, monkeypatch):
    rig = build(make, media_dir, scratch_root, listing="No rust.")
    before = rig.snapshot()
    config = load_report_config()
    drifted = replace(config, scoring=config.scoring.model_copy(update={"version": "future-version"}))
    monkeypatch.setattr(snapshots, "load_report_config", lambda: drifted)
    after = rig.snapshot()
    assert after.score == before.score and after.claims == before.claims
    assert after.free_narration == before.free_narration and after.paid_narration == before.paid_narration
    with rig.h.db() as db, pytest.raises(SnapshotIntegrityError):
        load_snapshot(db, rig.revision(), config=drifted)


def test_claim_write_serializes_with_ready_transition_then_is_rejected(make, media_dir, scratch_root):
    rig = build(make, media_dir, scratch_root, listing="No rust.", drain=False)
    rig.advance_to("render")
    revision = rig.revision()
    with rig.h.db() as ready, rig.h.db() as child:
        ready.execute(update(ReportRevision).where(ReportRevision.id == revision.id).values(
            state="ready", rendered_at=datetime.now(timezone.utc)))
        child.execute(text("SET LOCAL lock_timeout = '100ms'"))
        mutation = update(Claim).where(Claim.report_revision_id == revision.id).values(rationale="Concurrent rewrite")
        with pytest.raises(OperationalError) as failure:
            child.execute(mutation)
        assert failure.value.orig.sqlstate == "55P03"
        child.rollback()
        ready.commit()
        with pytest.raises(IntegrityError):
            child.execute(mutation)
        child.rollback()
    assert all(claim.rationale != "Concurrent rewrite" for claim in rig.snapshot().claims)
