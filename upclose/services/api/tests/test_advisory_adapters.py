"""Advisor provider switch, env handling, request shapes, retries, secret hygiene. Mocked transports only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.advisor import (AdvisorProviderError, get_advisor_adapter)
from app.adapters.advisor.config import AdvisorConfigError, load_env
from app.adapters.advisor.fixture import FixtureAdvisor, MODEL as FIXTURE_MODEL
from app.adapters.advisor.gemini import GeminiAdvisor
from app.adapters.advisor.meta import MetaAdvisor
from app.contracts.sources import (DtcEntry, IrregularWindow, ObdSummary, RigTelemetrySummary,
                                   TemperatureSummary)
from app.domain.advisory import load_advisory_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_advisory_config(ROOT / "config/advisory.v1.json")
HASH = "0" * 64
KEY = "sk_test_SECRET123"
VIN = "SYNTHETC00000UPCA"
META_ENV = {"ADVISOR_PROVIDER": "meta", "MODEL_API_KEY": KEY, "ADVISOR_META_MODEL": "muse-spark-1.3"}
def ALLOW(_prepared):
    return True


GEMINI_ENV = {"ADVISOR_PROVIDER": "gemini", "GEMINI_API_KEY": KEY, "ADVISOR_GEMINI_MODEL": "gemini-3.6-flash"}


def summaries():
    rig = RigTelemetrySummary(
        config_hash=HASH, provenance="synthetic", sample_count=4000, duration_s=10.0,
        state="assessed", dominant_hz=25.0, rpm_estimate=750, cylinders=4, roughness=0.31,
        irregular_windows=(IrregularWindow(t_ms=2000, rms_mg=120.0),),
        temperature=TemperatureSummary(start_c=21.4, rise_c=5.6, engine_warm_at_start=False),
        flags=("irregular_vibration", "rough_vibration"), flag_count=2)
    obd = ObdSummary(
        config_hash=HASH, provenance="synthetic", vin=VIN, mil_on=True,
        dtcs=(DtcEntry(code="P0301", status="stored"),),
        incomplete_monitors=("catalyst", "evap"), codes_recently_cleared=True,
        cold_start=True, coolant_c=24.0, distance_since_codes_cleared_km=12,
        warmups_since_codes_cleared=1,
        flags=("mil_on", "trouble_codes", "codes_recently_cleared"), flag_count=3)
    return (rig, obd)


def valid_text():
    return json.dumps({
        "questions_for_seller": [{"text": "Ask about P0301.", "cites": ["obd_snapshot:dtc:P0301"]}],
        "mechanic_checks": [{"text": "Recheck.", "cites": ["rig_telemetry:irregular_windows"]}],
        "summary": "Simulated flags; ask and check."})


def meta_ok(text):
    def run(url, body, headers, timeout):
        assert url == "https://api.meta.ai/v1/responses"
        payload = json.loads(body)
        assert payload["model"] == "muse-spark-1.3"
        content = payload["input"][0]["content"]
        assert content[0]["type"] == "input_text" and "uncalibrated" in content[0]["text"].lower()
        assert headers["Authorization"] == "Bearer " + KEY
        assert timeout <= 30.0
        return 200, json.dumps({"status": "completed", "model": "muse-spark-1.3",
                                "id": "resp_1", "usage": {"input_tokens": 900, "output_tokens": 120,
                                                  "input_tokens_details": {"cached_tokens": 0}},
                                "output": [{"content": [{"type": "output_text",
                                                        "text": text}]}]}).encode()
    return run


def gemini_ok(text):
    def run(url, body, headers, timeout):
        assert url == ("https://generativelanguage.googleapis.com/v1beta/models/"
                       "gemini-3.6-flash:generateContent")
        payload = json.loads(body)
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseSchema" in payload["generationConfig"]
        assert "systemInstruction" in payload
        assert payload["contents"][0]["parts"][0]["text"]
        assert headers["x-goog-api-key"] == KEY
        assert "key=" not in url  # key travels in the header, never the URL
        return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                                "responseId": "g1", "usageMetadata": {"promptTokenCount": 900,
                                                                       "candidatesTokenCount": 100,
                                                                       "thoughtsTokenCount": 20}}).encode()
    return run


# ---- switch and env ---------------------------------------------------------------------------------


def test_default_is_fixture_and_unknown_rejected():
    assert isinstance(get_advisor_adapter({}), FixtureAdvisor)
    with pytest.raises(AdvisorConfigError):
        load_env({"ADVISOR_PROVIDER": "other"})


def test_provider_switch_picks_right_adapter():
    assert isinstance(get_advisor_adapter(META_ENV), MetaAdvisor)
    assert isinstance(get_advisor_adapter(GEMINI_ENV), GeminiAdvisor)


@pytest.mark.parametrize("env,missing", [
    (META_ENV, "MODEL_API_KEY"), (META_ENV, "ADVISOR_META_MODEL"),
    (GEMINI_ENV, "GEMINI_API_KEY"), (GEMINI_ENV, "ADVISOR_GEMINI_MODEL")])
def test_missing_values_fail_closed_naming_variable(env, missing):
    with pytest.raises(AdvisorConfigError, match=missing):
        load_env({k: v for k, v in env.items() if k != missing})


def test_meta_base_must_be_https():
    with pytest.raises(AdvisorConfigError):
        load_env({**META_ENV, "META_API_BASE": "http://insecure.example"})


def test_env_repr_hides_key():
    assert KEY not in repr(load_env(META_ENV)) and KEY not in repr(load_env(GEMINI_ENV))


# ---- fixture ----------------------------------------------------------------------------------------


def test_fixture_deterministic_template_output():
    first = get_advisor_adapter({}).advise(summaries())
    second = FixtureAdvisor(CONFIG).advise(summaries())
    assert first == second
    assert first["provider"] == "fixture" and first["model"] == FIXTURE_MODEL
    assert first["prompt_version"] == "advisory-v1"
    assert first["questions_for_seller"] and first["mechanic_checks"]
    assert len(first["summary"]) <= 600
    for group in (first["questions_for_seller"], first["mechanic_checks"]):
        for item in group:
            assert item["text"] and item["cites"]
    assert "template" in first["summary"].lower() or "simulated" in first["summary"].lower()


def test_fixture_empty_input_is_not_available():
    wire = FixtureAdvisor(CONFIG).advise(())
    assert wire["status"] == "not_available" and wire["reason"]


def test_fixture_output_carries_no_vin():
    assert VIN not in json.dumps(FixtureAdvisor(CONFIG).advise(summaries()))


# ---- live-shaped (mocked) -----------------------------------------------------------------------------


def test_meta_request_body_and_valid_response():
    wire = MetaAdvisor(load_env(META_ENV), CONFIG, meta_ok(valid_text())).advise(summaries(), grant=ALLOW)
    assert wire["provider"] == "meta" and wire["model"] == "muse-spark-1.3"
    assert wire["questions_for_seller"][0]["cites"] == ["obd_snapshot:dtc:P0301"]


def test_gemini_request_body_and_valid_response():
    wire = GeminiAdvisor(load_env(GEMINI_ENV), CONFIG, gemini_ok(valid_text())).advise(summaries(), grant=ALLOW)
    assert wire["provider"] == "gemini" and wire["model"] == "gemini-3.6-flash"
    assert wire["mechanic_checks"][0]["cites"] == ["rig_telemetry:irregular_windows"]


def test_gemini_malformed_model_id_rejected():
    with pytest.raises(AdvisorProviderError):
        GeminiAdvisor(load_env({**GEMINI_ENV, "ADVISOR_GEMINI_MODEL": "bad model!"}), CONFIG)


def test_500_is_not_retried_inside_the_adapter():
    calls = []

    def run(url, body, headers, timeout):
        calls.append(url)
        return 500, b"{}"

    with pytest.raises(AdvisorProviderError, match="HTTP 500"):
        MetaAdvisor(load_env(META_ENV), CONFIG, run).advise(summaries(), grant=ALLOW)
    assert len(calls) == 1


def test_no_retry_on_400_and_no_secrets_in_error():
    calls = []

    def run(url, body, headers, timeout):
        calls.append(url)
        return 400, json.dumps({"error": f"bad key {KEY}"}).encode()

    with pytest.raises(AdvisorProviderError) as exc:
        GeminiAdvisor(load_env(GEMINI_ENV), CONFIG, run).advise(summaries(), grant=ALLOW)
    assert len(calls) == 1
    assert "400" in str(exc.value) and KEY not in str(exc.value)


def test_timeout_surfaces_sanitized_after_one_attempt():
    calls = []

    def run(url, body, headers, timeout):
        calls.append(1)
        raise AdvisorProviderError("advisor provider request timed out", retryable=True)

    with pytest.raises(AdvisorProviderError) as exc:
        MetaAdvisor(load_env(META_ENV), CONFIG, run).advise(summaries(), grant=ALLOW)
    assert "timed out" in str(exc.value) and len(calls) == 1


def test_generic_transport_failure_sanitized_without_key_or_vin():
    def run(url, body, headers, timeout):
        raise RuntimeError(f"boom {KEY} {VIN}")

    with pytest.raises(AdvisorProviderError) as exc:
        MetaAdvisor(load_env(META_ENV), CONFIG, run).advise(summaries(), grant=ALLOW)
    assert KEY not in str(exc.value) and VIN not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


def test_meta_unauthorized_names_no_secrets():
    def run(url, body, headers, timeout):
        return 401, b"{}"

    with pytest.raises(AdvisorProviderError) as exc:
        MetaAdvisor(load_env(META_ENV), CONFIG, run).advise(summaries(), grant=ALLOW)
    assert KEY not in str(exc.value)


def test_meta_incomplete_mismatched_or_malformed_rejected():
    cases = [
        (200, {"status": "incomplete", "model": "muse-spark-1.3", "output": []}),
        (200, {"status": "completed", "model": "someone-else",
               "output": [{"content": [{"type": "output_text", "text": valid_text()}]}]}),
        (200, {"status": "completed", "model": "muse-spark-1.3", "output": []}),
    ]
    for status, payload in cases:
        with pytest.raises(AdvisorProviderError):
            MetaAdvisor(load_env(META_ENV), CONFIG,
                        lambda u, b, h, t: (status, json.dumps(payload).encode())).advise(summaries(), grant=ALLOW)


def test_gemini_blocked_prompt_rejected():
    def run(url, body, headers, timeout):
        return 200, json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}).encode()

    with pytest.raises(AdvisorProviderError, match="blocked"):
        GeminiAdvisor(load_env(GEMINI_ENV), CONFIG, run).advise(summaries(), grant=ALLOW)


def test_invalid_model_output_becomes_not_available_not_error():
    wire = MetaAdvisor(load_env(META_ENV), CONFIG,
                       meta_ok('{"questions_for_seller": [], "mechanic_checks": [], '
                               '"summary": "nothing"}')).advise(summaries(), grant=ALLOW)
    assert wire["status"] == "not_available"
