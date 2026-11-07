"""Regenerate small, synthetic contract examples; no real vehicle evidence."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.contracts import (
    ConceptRun, Coverage, Finding, FreeReport, PaidReport, ScoringInput, VerificationGroup,
)


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def save(name: str, model: str, data: dict, description: str, *, valid: bool = True) -> None:
    if valid:
        data = globals()[model].model_validate(data).model_dump(mode="json")
    wrapper = {"metadata": {"provenance": "synthetic", "description": description}, "model": model, "valid": valid, "data": data}
    Path(__file__).with_name(name + ".json").write_text(json.dumps(wrapper, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    clean_coverage = [
        {"run_id": uid(2), "zone": "driver_side", "concept": concept, "state": "assessed"}
        for concept in ["rust", "paint_bubbling", "dent", "scratch"]
    ]
    save("clean_assessed_zone", "ScoringInput", {"session_id": uid(1), "run_ids": [uid(2)], "findings": [], "coverage": clean_coverage}, "Synthetic driver-side required concepts assessed with zero findings; not an accuracy evaluation.")
    save("missing_part", "Coverage", {"run_id": uid(2), "zone": "underbody", "concept": "rust", "state": "not_assessed", "reason": "part_skipped"}, "Skipped footage is not assessed, never clean.")
    save("failed_concept", "ConceptRun", {"id": uid(5), "run_id": uid(2), "part_id": uid(3), "zone": "driver_side", "concept": "rust", "prompt_id": "synthetic-rust-v1", "state": "failed", "reason": "synthetic_timeout"}, "Synthetic model timeout; no paid call was made.")
    finding = {"id": uid(4), "session_id": uid(1), "run_id": uid(2), "part_id": uid(3), "concept_run_id": uid(5), "part_no": 1, "zone": "driver_side", "track_id": "synthetic-track-1", "status": "verified", "concept": "rust", "evidence": {"frame_key": "synthetic/frame-0.png", "frame_index": 0, "frame_width": 1280, "frame_height": 720, "bbox": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.1}}, "severity": 5, "confidence": 0.8, "evidence_note": "Synthetic rust patch on named rocker.", "component_id": "driver_rocker", "subzone": "rocker", "flags": ["structural_rust_review"]}
    save("rusty_zone", "Finding", finding, "Handwritten synthetic rust finding; confidence is uncalibrated.")
    verdict = {"finding_id": uid(4), "verdict": "confirmed", "concept": "rust", "severity": 5, "evidence_note": "Synthetic retained patch."}
    duplicate = {"finding_id": uid(6), "verdict": "duplicate", "concept": "rust", "duplicate_of": uid(4), "evidence_note": "Synthetic same-frame duplicate."}
    save("duplicate_candidate", "VerificationGroup", {"candidate_ids": [uid(4), uid(6)], "verdicts": [verdict, duplicate]}, "Two synthetic candidates resolve to one retained finding.")
    save("malformed_verdict", "VerificationGroup", {"candidate_ids": [uid(4)], "verdicts": [{**verdict, "severity": None}]}, "Intentionally invalid: confirmed verdict omits severity.", valid=False)
    public_finding = {key: finding[key] for key in ["id", "zone", "concept", "severity", "evidence_note", "subzone", "component_id", "flags"]}
    public_finding.update(bbox=finding["evidence"]["bbox"], evidence_available=False, confidence=finding["confidence"])
    coverage = [{"zone": "driver_side", "state": "assessed"}, {"zone": "underbody", "state": "not_assessed", "reason": "part_skipped"}]
    common = {"slug": "synthetic-report", "revision_id": uid(7), "state": "ready", "coverage": coverage, "findings": [public_finding]}
    save("free_report", "FreeReport", {**common, "scope": "free", "part1_score": 85, "locked_zones": [{"zone": "passenger_side", "finding_count": 0}], "part1_summary": "Synthetic Part-1 rust finding. Evidence image withheld."}, "Synthetic free DTO; no paid evidence or paid fields.")
    save("paid_report", "PaidReport", {**common, "scope": "paid", "scores": {"headline": None, "zones": [{"zone": "driver_side", "state": "assessed", "score": 85}], "config_hash": "0" * 64}, "repairs": {"items": [{"operation": None, "component_id": "driver_rocker", "severity": 5, "finding_ids": [uid(4)], "low_cents": None, "high_cents": None, "reason": "structural_rust_not_estimated"}], "subtotal_low_cents": 0, "subtotal_high_cents": 0, "unestimated_count": 1, "table_version": "synthetic-planning-v1"}, "claims": [], "narration": []}, "Synthetic paid DTO; unknown repair remains null, not a zero-cost estimate.")


if __name__ == "__main__":
    main()
