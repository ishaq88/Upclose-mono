import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app import contracts
from app.contracts import (
    Artifact, BoundingBox, ClaimGroup, CreateSessionRequest, CreateUploadRequest,
    Finding, FixtureMetadata, FreeReport, ScoringInput, UploadUrlsRequest,
    VerificationGroup, VerificationVerdict, ZoneScore,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_labeled_contract_fixtures(path: Path) -> None:
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    assert FixtureMetadata.model_validate(wrapper["metadata"]).provenance == "synthetic"
    model = getattr(contracts, wrapper["model"])
    if wrapper["valid"]:
        model.model_validate(wrapper["data"])
    else:
        with pytest.raises(ValidationError):
            model.model_validate(wrapper["data"])


@pytest.mark.parametrize("change", [
    {"zone": "front_rear"}, {"concept": "rust | dent"}, {"severity": 0},
    {"severity": 6}, {"severity": True}, {"confidence": float("nan")},
    {"confidence": 1.1}, {"component_id": "invented_panel"}, {"subzone": "invented"},
    {"part_no": 2}, {"unexpected": "field"}, {"id": "not-a-uuid"},
    {"status": "candidate"}, {"coverage_pct": 12},
])
def test_finding_rejects_invalid_contract_fields(change: dict) -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate({**fixture("rusty_zone"), **change})


@pytest.mark.parametrize("bbox", [
    {"x": -0.1, "y": 0, "w": 0.1, "h": 0.1},
    {"x": 0.9, "y": 0, "w": 0.2, "h": 0.1},
    {"x": 0, "y": 0.9, "w": 0.1, "h": 0.2},
    {"x": 0, "y": 0, "w": 0, "h": 0.1},
    {"x": 0, "y": 0, "w": 0.1, "h": float("inf")},
])
def test_rejects_invalid_geometry(bbox: dict) -> None:
    with pytest.raises(ValidationError):
        BoundingBox.model_validate(bbox)


@pytest.mark.parametrize("changes", [
    {"severity": None}, {"verdict": "artifact"},
    {"verdict": "duplicate", "severity": None},
    {"duplicate_of": "00000000-0000-4000-8000-000000000099"},
])
def test_verdict_cross_field_constraints(changes: dict) -> None:
    data = fixture("duplicate_candidate")["verdicts"][0]
    with pytest.raises(ValidationError):
        VerificationVerdict.model_validate({**data, **changes})


@pytest.mark.parametrize("case", ["foreign", "missing", "repeated", "cycle", "self"])
def test_group_rejects_invalid_references(case: str) -> None:
    data = fixture("duplicate_candidate")
    if case == "foreign":
        data["verdicts"][1]["duplicate_of"] = "00000000-0000-4000-8000-000000000099"
    elif case == "missing":
        data["verdicts"].pop()
    elif case == "repeated":
        data["verdicts"].append(data["verdicts"][0])
    elif case == "cycle":
        first, second = data["verdicts"]
        first.update(verdict="duplicate", severity=None, duplicate_of=second["finding_id"])
    else:
        data["verdicts"][1]["duplicate_of"] = data["verdicts"][1]["finding_id"]
    with pytest.raises(ValidationError):
        VerificationGroup.model_validate(data)


def test_claim_evidence_whitelist_and_ownership() -> None:
    finding = fixture("rusty_zone")
    claim = {"claim_index": 0, "text": "Synthetic claim", "verdict": "contradicted", "finding_ids": [finding["id"]], "evidence_frame_keys": [finding["evidence"]["frame_key"]], "rationale": "Synthetic evidence"}
    ClaimGroup.model_validate({"findings": [finding], "claims": [claim]})
    foreign = {**finding, "id": "00000000-0000-4000-8000-000000000099", "session_id": "00000000-0000-4000-8000-000000000098"}
    for findings, changes in [
        ([finding, foreign], {}), ([finding, finding], {}),
        ([finding], {"finding_ids": [foreign["id"]]}),
        ([finding], {"evidence_frame_keys": ["foreign/frame.png"]}),
        ([{**finding, "status": "unverified", "severity": None}], {}),
    ]:
        with pytest.raises(ValidationError):
            ClaimGroup.model_validate({"findings": findings, "claims": [{**claim, **changes}]})


def test_cross_run_claims_in_same_session_are_allowed() -> None:
    first = fixture("rusty_zone")
    second = {**first, "id": "00000000-0000-4000-8000-000000000099", "run_id": "00000000-0000-4000-8000-000000000098"}
    ClaimGroup.model_validate({"findings": [first, second], "claims": []})


def test_scoring_input_rejects_foreign_session_and_duplicate_references() -> None:
    finding = fixture("rusty_zone")
    data = {"session_id": finding["session_id"], "run_ids": [finding["run_id"]], "findings": [finding], "coverage": []}
    ScoringInput.model_validate(data)
    with pytest.raises(ValidationError):
        ScoringInput.model_validate({**data, "session_id": "00000000-0000-4000-8000-000000000099"})
    duplicate = {**finding, "id": "00000000-0000-4000-8000-000000000098", "status": "rejected", "severity": None, "duplicate_of": "00000000-0000-4000-8000-000000000099"}
    with pytest.raises(ValidationError):
        ScoringInput.model_validate({**data, "findings": [finding, duplicate]})


def test_decimal_scores_round_only_on_serialization() -> None:
    score = ZoneScore(zone="driver_side", state="assessed", score=Decimal("94.45"))
    assert score.score == Decimal("94.45")
    assert json.loads(score.model_dump_json())["score"] == 94.5
    assert ZoneScore.model_json_schema(mode="serialization")["properties"]["score"]["anyOf"][0]["type"] == "number"
    with pytest.raises(ValidationError):
        ZoneScore(zone="driver_side", state="partial", score=85)


@pytest.mark.parametrize("field", ["headline", "scores", "repairs", "claims", "narration"])
def test_free_dto_cannot_contain_paid_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        FreeReport.model_validate({**fixture("free_report"), field: []})


def test_free_dto_cannot_include_other_zone_finding() -> None:
    data = fixture("free_report")
    data["findings"][0]["zone"] = "passenger_side"
    with pytest.raises(ValidationError):
        FreeReport.model_validate(data)
    rendered = FreeReport.model_validate(fixture("free_report")).model_dump(mode="json")
    assert not ({"claims", "narration", "scores", "repairs", "headline"} & rendered.keys())
    assert not ({"frame_key", "mask_key", "run_id"} & rendered["findings"][0].keys())


def test_timezone_must_be_aware_and_normalizes_to_utc() -> None:
    artifact = {"id": "00000000-0000-4000-8000-000000000001", "session_id": "00000000-0000-4000-8000-000000000002", "key": "synthetic/frame.png", "sha256": "0" * 64, "privacy_state": "withheld", "created_at": "2026-10-28T12:00:00+05:00"}
    assert Artifact.model_validate(artifact).model_dump(mode="json")["created_at"] == "2026-10-28T07:00:00Z"
    with pytest.raises(ValidationError):
        Artifact.model_validate({**artifact, "created_at": "2026-10-28T12:00:00"})
    with pytest.raises(ValidationError):
        Artifact.model_validate({**artifact, "key": "https://example.com/private?signed=secret"})


def test_api_inputs_are_bounded_and_explicit() -> None:
    CreateSessionRequest(purpose="buyer", consent=True, consent_version="v1")
    for changes in [{"consent": False}, {"purpose": "seller"}, {"listing_text": "x" * 10001}]:
        with pytest.raises(ValidationError):
            CreateSessionRequest.model_validate({"purpose": "buyer", "consent": True, "consent_version": "v1", **changes})
    for length in [0, 200 * 1024 * 1024 + 1, True]:
        with pytest.raises(ValidationError):
            CreateUploadRequest(part_no=1, content_type="video/mp4", content_length=length)
    for parts in [[], [1, 1], list(range(1, 12)), [26]]:
        with pytest.raises(ValidationError):
            UploadUrlsRequest(part_numbers=parts)


def test_scoring_input_accepts_multiple_whitelisted_part_runs() -> None:
    first = fixture("rusty_zone")
    second = {**first, "id": "00000000-0000-4000-8000-000000000099", "run_id": "00000000-0000-4000-8000-000000000098", "part_id": "00000000-0000-4000-8000-000000000097", "part_no": 2, "zone": "passenger_side"}
    data = {"session_id": first["session_id"], "run_ids": [first["run_id"], second["run_id"]], "findings": [first, second], "coverage": []}
    assert len(ScoringInput.model_validate(data).run_ids) == 2
    with pytest.raises(ValidationError):
        ScoringInput.model_validate({**data, "run_ids": [first["run_id"]]})


def test_public_findings_require_verified_severity_and_label_confidence() -> None:
    data = fixture("free_report")
    assert data["findings"][0]["confidence"] == 0.8
    assert "not an accuracy probability" in data["findings"][0]["confidence_label"]
    data["findings"][0]["severity"] = None
    with pytest.raises(ValidationError):
        FreeReport.model_validate(data)


def test_public_claims_exclude_storage_keys_and_validate_citations() -> None:
    from app.contracts import PaidReport, PublicClaim

    claim = {"claim_index": 0, "text": "Synthetic claim", "verdict": "contradicted", "finding_ids": [fixture("rusty_zone")["id"]], "rationale": "Synthetic evidence"}
    assert "evidence_frame_keys" not in PublicClaim.model_fields
    with pytest.raises(ValidationError):
        PublicClaim.model_validate({**claim, "evidence_frame_keys": ["private/raw/frame.png"]})
    data = fixture("paid_report")
    PaidReport.model_validate({**data, "claims": [claim]})
    claim["finding_ids"] = ["00000000-0000-4000-8000-000000000099"]
    with pytest.raises(ValidationError):
        PaidReport.model_validate({**data, "claims": [claim]})
