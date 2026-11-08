"""Advisory block: validation, provider failure -> not_available, labels, VIN and key hygiene."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.adapters.advisor import AdvisorProviderError
from app.adapters.advisor.config import load_env
from app.adapters.advisor.gemini import GeminiAdvisor
from app.briefing.advisory_block import ADVISORY_LABEL, AdvisoryBlock, build_advisory_block
from tests.test_advisory_adapters import CONFIG, GEMINI_ENV
from tests.test_briefing_common import KEY, summaries

SETTINGS = SimpleNamespace(model_mode="fixture")
VIN = "SYNTHETC00000UPCA"


class Stub:
    """Class name is not 'Fixture...', so it counts as a live provider."""

    def __init__(self, result=None, exc: Exception | None = None, delay: float = 0.0) -> None:
        self.result, self.exc, self.delay, self.calls = result, exc, delay, 0

    def advise(self, _summaries):
        self.calls += 1
        time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def good(provider="gemini"):
    return {"questions_for_seller": [{"text": "Ask about P0301.", "cites": ["obd_snapshot:dtc:P0301"]}],
            "mechanic_checks": [{"text": "Recheck vibration.", "cites": ["rig_telemetry:irregular_windows"]}],
            "summary": "Ask and check.", "provider": provider, "model": "m-1", "prompt_version": "advisory-v1"}


def test_fixture_mode_block_is_labeled_and_fixture_provenance():
    block = build_advisory_block(summaries(), {"make": "Synth", "model": "One", "year": 2012, "vin": VIN},
                                 SETTINGS, environ={})
    assert block.status == "available" and block.label == ADVISORY_LABEL == "AI advisory, not a finding"
    assert block.provenance == "fixture" and block.provider == "fixture" and block.model
    assert block.questions_for_seller and block.mechanic_checks
    assert block.vehicle == "2012 Synth One"
    assert VIN not in str(block.to_json())


def test_block_roundtrips_and_is_frozen():
    block = build_advisory_block(summaries(), None, SETTINGS, environ={})
    assert AdvisoryBlock.from_json(block.to_json()) == block
    with pytest.raises(ValidationError):
        block.summary = "changed"


def test_from_json_rejects_inconsistent_blocks():
    raw = build_advisory_block(summaries(), None, SETTINGS, environ={}).to_json()
    with pytest.raises(ValidationError):
        AdvisoryBlock.from_json({**raw, "label": "Finding"})
    with pytest.raises(ValidationError):
        AdvisoryBlock.from_json({**raw, "questions_for_seller": [], "mechanic_checks": []})
    with pytest.raises(ValidationError):
        AdvisoryBlock.from_json({**raw, "provider": "gemini"})  # fixture provenance, other provider
    with pytest.raises(ValidationError):
        AdvisoryBlock.from_json({**raw, "extra": 1})


def test_live_style_result_validates():
    block = build_advisory_block(summaries(), None, SETTINGS, adapter=Stub(good()))
    assert block.status == "available" and block.provenance == "live" and block.model == "m-1"


@pytest.mark.parametrize("exc", [AdvisorProviderError("advisor provider returned HTTP 503"),
                                 RuntimeError("boom"), ValueError("bad")])
def test_provider_failure_is_not_available_never_raises(exc):
    block = build_advisory_block(summaries(), None, SETTINGS, adapter=Stub(exc=exc))
    assert block.status == "not_available" and block.reason
    assert block.label == ADVISORY_LABEL and not block.questions_for_seller


def test_timeout_is_not_available():
    block = build_advisory_block(summaries(), None, SETTINGS, adapter=Stub(good(), delay=1.0),
                                 deadline_seconds=0.1)
    assert block.status == "not_available" and "timed out" in block.reason


def test_provider_not_available_result_and_garbage_result():
    na = {"status": "not_available", "reason": "no item cited evidence", "provider": "gemini",
          "model": "m-1", "prompt_version": "advisory-v1"}
    assert build_advisory_block(summaries(), None, SETTINGS, adapter=Stub(na)).status == "not_available"
    assert build_advisory_block(summaries(), None, SETTINGS, adapter=Stub("nope")).status == "not_available"
    bad = {**good(), "questions_for_seller": [{"text": "", "cites": []}]}
    assert build_advisory_block(summaries(), None, SETTINGS, adapter=Stub(bad)).status == "not_available"


def test_no_evidence_or_replay_mode_makes_no_call():
    stub = Stub(good())
    assert build_advisory_block((), None, SETTINGS, adapter=stub).status == "not_available"
    assert build_advisory_block(summaries(), None, SimpleNamespace(model_mode="replay"),
                                environ={}).status == "not_available"
    assert stub.calls == 0


def test_budget_guard_blocks_live_provider_only():
    stub = Stub(good())
    block = build_advisory_block(summaries(), None, SETTINGS, adapter=stub, budget_ok=lambda: False)
    assert block.status == "not_available" and "budget" in block.reason and stub.calls == 0
    fixture = build_advisory_block(summaries(), None, SETTINGS, environ={}, budget_ok=lambda: False)
    assert fixture.status == "available"


def test_misconfigured_live_provider_never_leaks_key():
    env = {"ADVISOR_PROVIDER": "gemini", "GEMINI_API_KEY": KEY}  # model id missing
    block = build_advisory_block(summaries(), None, SETTINGS, environ=env)
    assert block.status == "not_available" and KEY not in str(block.to_json())


def test_key_in_underlying_error_text_stays_out_of_block():
    def transport(url, body, headers, timeout):
        raise RuntimeError(f"connection failed for {headers['x-goog-api-key']}")
    adapter = GeminiAdvisor(load_env(GEMINI_ENV), CONFIG, transport)
    block = build_advisory_block(summaries(), None, SETTINGS, adapter=adapter)
    assert block.status == "not_available"
    assert KEY not in str(block.to_json())
