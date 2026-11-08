"""Prepared, granted, single-dispatch live calls: grant, digest, caps, one transport, usage evidence."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.advisor import AdvisorProviderError
from app.adapters.advisor.config import load_env as advisor_env
from app.adapters.advisor.meta import MetaAdvisor
from app.adapters.storage import StorageError
from app.adapters.voice.config import load_env as voice_env
from app.adapters.voice.elevenlabs import ElevenLabsVoice
from app.briefing.advisory_block import build_advisory_block
from app.briefing.dispatch import (DispatchDenied, PreparedRequestError, PreparedUnits, UsageEvidence,
                                   prepare_request, utf16_units)
from app.briefing.narration_audio import NarrationAudioError, build_narration_audio
from app.briefing.voice_guide import VoiceGuide
from app.contracts.reports import FreeReport
from tests.test_advisory_adapters import CONFIG, META_ENV, meta_ok, summaries, valid_text
from tests.test_briefing_common import ELEVEN_ENV, FIXTURES, KEY, SESSION, MemoryStorage, allow, paid_report


class Ledger:
    """Stand-in for the server: records grants and evidence."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed, self.granted, self.evidence = allowed, [], []

    def grant(self, prepared) -> bool:
        self.granted.append(prepared)
        return self.allowed

    def record(self, prepared, evidence: UsageEvidence) -> None:
        self.evidence.append((prepared, evidence))


class Transport:
    def __init__(self, fn) -> None:
        self.fn, self.calls = fn, 0

    def __call__(self, url, body, headers, timeout):
        self.calls += 1
        return self.fn(url, body, headers, timeout)


def meta(transport):
    return MetaAdvisor(advisor_env(META_ENV), CONFIG, transport)


def voice_with(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)
    return ElevenLabsVoice(voice_env(ELEVEN_ENV), httpx.MockTransport(wrapped)), seen


def eleven(status=200, content=b"ID3mp3"):
    return voice_with(lambda request: httpx.Response(status, content=content))


# ---- advisory -----------------------------------------------------------------------------------


def test_prepared_advisory_request_is_capped_and_bound_to_its_body():
    prepared = meta(Transport(meta_ok(valid_text()))).prepare(summaries())
    body = json.loads(prepared.body)
    assert body["max_output_tokens"] == 2048 and body["store"] is False
    assert prepared.units.output_tokens == 2048 and prepared.units.input_tokens >= len(prepared.body)
    assert len(prepared.body) <= 16 * 1024 and KEY not in prepared.url


def test_denied_grant_means_no_transport():
    ledger, transport = Ledger(allowed=False), Transport(meta_ok(valid_text()))
    with pytest.raises(DispatchDenied):
        meta(transport).advise(summaries(), grant=ledger.grant, record=ledger.record)
    assert transport.calls == 0 and len(ledger.granted) == 1 and ledger.evidence == []


def test_raising_grant_is_a_denial():
    transport = Transport(meta_ok(valid_text()))

    def boom(_prepared):
        raise RuntimeError("db down")
    with pytest.raises(DispatchDenied):
        meta(transport).advise(summaries(), grant=boom)
    assert transport.calls == 0


def test_missing_grant_refuses_before_transport():
    transport = Transport(meta_ok(valid_text()))
    with pytest.raises(DispatchDenied):
        meta(transport).advise(summaries())
    assert transport.calls == 0


def test_modified_body_is_rejected_before_transport():
    transport = Transport(meta_ok(valid_text()))
    adapter = meta(transport)
    prepared = adapter.prepare(summaries())
    tampered = dataclasses.replace(prepared, body=prepared.body.replace(b"muse-spark-1.3", b"muse-spark-1.1"))
    ledger = Ledger()
    with pytest.raises(PreparedRequestError):
        adapter.advise_prepared(tampered, summaries(), grant=ledger.grant, record=ledger.record)
    assert transport.calls == 0 and ledger.granted == []


def test_uncapped_or_oversized_requests_cannot_be_prepared():
    body = json.dumps({"model": "m", "input": "x"}).encode()  # no max_output_tokens in the body
    with pytest.raises(PreparedRequestError):
        prepare_request(kind="advisory", provider="meta", model="m", url="https://x", body=body,
                        units=PreparedUnits(input_tokens=len(body), output_tokens=2048))
    big = json.dumps({"max_output_tokens": 2048, "pad": "x" * 17000}).encode()
    with pytest.raises(PreparedRequestError):
        prepare_request(kind="advisory", provider="meta", model="m", url="https://x", body=big,
                        units=PreparedUnits(input_tokens=len(big), output_tokens=2048))
    ok = json.dumps({"max_output_tokens": 4096}).encode()
    with pytest.raises(PreparedRequestError):
        prepare_request(kind="advisory", provider="meta", model="m", url="https://x", body=ok,
                        units=PreparedUnits(input_tokens=len(ok), output_tokens=4096))


def test_one_transport_attempt_and_usage_recorded_once():
    ledger, transport = Ledger(), Transport(meta_ok(valid_text()))
    wire = meta(transport).advise(summaries(), grant=ledger.grant, record=ledger.record)
    assert wire.get("status") != "not_available" and transport.calls == 1
    (prepared, evidence), = ledger.evidence
    assert evidence.outcome == "billed" and evidence.input_tokens == 900 and evidence.output_tokens == 120
    assert evidence.cached_input_tokens == 0 and evidence.provider_request_id == "resp_1"
    assert evidence.body_sha256 == prepared.body_sha256 and evidence.http_status == 200
    assert KEY not in repr(evidence)


def test_server_error_is_unknown_not_retried_and_not_zero():
    ledger, transport = Ledger(), Transport(lambda *a: (503, b"{}"))
    with pytest.raises(AdvisorProviderError):
        meta(transport).advise(summaries(), grant=ledger.grant, record=ledger.record)
    (_, evidence), = ledger.evidence
    assert transport.calls == 1 and evidence.outcome == "unknown" and evidence.input_tokens is None
    assert evidence.http_status == 503


def test_timeout_stays_unknown():
    def timeout(*_a):
        raise AdvisorProviderError("advisor provider request timed out", retryable=True, code="timeout")
    ledger, transport = Ledger(), Transport(timeout)
    with pytest.raises(AdvisorProviderError, match="timed out"):
        meta(transport).advise(summaries(), grant=ledger.grant, record=ledger.record)
    (_, evidence), = ledger.evidence
    assert transport.calls == 1 and evidence.outcome == "unknown" and evidence.failure_code == "timeout"


def test_usage_is_recorded_even_when_the_answer_is_invalid():
    ledger = Ledger()
    bad = meta_ok('{"questions_for_seller": [], "mechanic_checks": [], "summary": "nothing"}')
    wire = meta(Transport(bad)).advise(summaries(), grant=ledger.grant, record=ledger.record)
    assert wire["status"] == "not_available"
    (_, evidence), = ledger.evidence
    assert evidence.outcome == "billed_invalid" and evidence.output_tokens == 120
    incomplete = {"status": "incomplete", "model": "muse-spark-1.3", "output": [],
                  "usage": {"input_tokens": 5, "output_tokens": 7}}
    ledger2 = Ledger()
    with pytest.raises(AdvisorProviderError):
        meta(Transport(lambda *a: (200, json.dumps(incomplete).encode()))).advise(
            summaries(), grant=ledger2.grant, record=ledger2.record)
    assert ledger2.evidence[0][1].outcome == "billed_invalid" and ledger2.evidence[0][1].output_tokens == 7


def test_response_without_usage_is_unknown_even_if_valid():
    ledger = Ledger()
    payload = {"status": "completed", "model": "muse-spark-1.3",
               "output": [{"content": [{"type": "output_text", "text": valid_text()}]}]}
    meta(Transport(lambda *a: (200, json.dumps(payload).encode()))).advise(
        summaries(), grant=ledger.grant, record=ledger.record)
    assert ledger.evidence[0][1].outcome == "unknown"


def test_recorder_fault_does_not_hide_the_result():
    def bad_record(_p, _e):
        raise RuntimeError("db down")
    wire = meta(Transport(meta_ok(valid_text()))).advise(summaries(), grant=allow, record=bad_record)
    assert wire.get("status") != "not_available"


def test_exhausted_deadline_sends_nothing():
    transport, ledger = Transport(meta_ok(valid_text())), Ledger()
    with pytest.raises(AdvisorProviderError, match="deadline"):
        meta(transport).advise(summaries(), grant=ledger.grant, deadline_seconds=0.0)
    assert transport.calls == 0 and ledger.granted == []


def test_block_denied_grant_is_not_available_without_transport():
    transport = Transport(meta_ok(valid_text()))
    block = build_advisory_block(summaries(), None, SimpleNamespace(model_mode="live"), adapter=meta(transport),
                                 grant=Ledger(allowed=False).grant)
    assert block.status == "not_available" and "denied" in block.reason and transport.calls == 0


# ---- narration ----------------------------------------------------------------------------------


def test_narration_denied_grant_means_no_transport():
    adapter, seen = eleven()
    ledger = Ledger(allowed=False)
    with pytest.raises(NarrationAudioError):
        build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter,
                              grant=ledger.grant, record=ledger.record)
    assert seen == [] and len(ledger.granted) == 1


def test_narration_without_grant_makes_no_transport():
    adapter, seen = eleven()
    with pytest.raises(NarrationAudioError, match="grant"):
        build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter)
    assert seen == []


def test_narration_single_transport_and_character_evidence():
    adapter, seen = eleven()
    ledger = Ledger()
    audio = build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter,
                                  grant=ledger.grant, record=ledger.record)
    assert len(seen) == 1 and audio.provenance == "live"
    (prepared, evidence), = ledger.evidence
    spoken = json.loads(prepared.body)["text"]
    assert prepared.kind == "narration" and prepared.provider == "elevenlabs"
    assert prepared.units == PreparedUnits(characters=utf16_units(spoken)) and prepared.units.characters <= 2500
    assert evidence.outcome == "billed" and evidence.characters == prepared.units.characters
    assert evidence.input_tokens is None and spoken not in repr(evidence) and KEY not in repr(evidence)


def test_narration_cache_hit_makes_no_grant_and_no_transport():
    adapter, seen = eleven()
    storage, ledger = MemoryStorage(), Ledger()
    build_narration_audio(paid_report(), SESSION, storage, environ=ELEVEN_ENV, adapter=adapter, grant=ledger.grant,
                          record=ledger.record)
    second = build_narration_audio(paid_report(), SESSION, storage, environ=ELEVEN_ENV, adapter=adapter,
                                   grant=ledger.grant, record=ledger.record)
    assert second.cache_hit and len(seen) == 1 and len(ledger.granted) == 1
    third = build_narration_audio(paid_report(), SESSION, storage, environ=ELEVEN_ENV, adapter=adapter)
    assert third.cache_hit and len(seen) == 1  # a hit needs no grant at all


def test_unreadable_cache_never_triggers_a_paid_call():
    class Broken(MemoryStorage):
        def head_size(self, key):
            raise StorageError("down")
    adapter, seen = eleven()
    with pytest.raises(NarrationAudioError, match="cache"):
        build_narration_audio(paid_report(), SESSION, Broken(), environ=ELEVEN_ENV, adapter=adapter, grant=allow)
    assert seen == []


def test_long_narration_is_bounded_to_2500_characters_and_oversize_requests_cannot_be_prepared():
    adapter, seen = eleven()
    ledger = Ledger()
    build_narration_audio(paid_report(["Statement. " * 80] * 4), SESSION, MemoryStorage(), environ=ELEVEN_ENV,
                          adapter=adapter, grant=ledger.grant, record=ledger.record)
    assert len(seen) == 1 and ledger.granted[0].units.characters <= 2500
    body = json.dumps({"text": "x" * 2501, "model_id": "m"}).encode()
    with pytest.raises(PreparedRequestError):
        prepare_request(kind="narration", provider="elevenlabs", model="m", url="https://x", body=body,
                        units=PreparedUnits(characters=2501), voice_id="v")
    short = json.dumps({"text": "x" * 100, "model_id": "m"}).encode()
    with pytest.raises(PreparedRequestError):  # understated characters
        prepare_request(kind="narration", provider="elevenlabs", model="m", url="https://x", body=short,
                        units=PreparedUnits(characters=10), voice_id="v")


def test_narration_http_error_and_timeout_are_unknown_and_single_attempt():
    ledger = Ledger()
    adapter, seen = eleven(status=503, content=b"{}")
    with pytest.raises(NarrationAudioError):
        build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter,
                              grant=ledger.grant, record=ledger.record)
    assert len(seen) == 1 and ledger.evidence[0][1].outcome == "unknown"
    assert ledger.evidence[0][1].characters is None and ledger.evidence[0][1].http_status == 503

    def slow(request):
        raise httpx.ReadTimeout(f"timed out {KEY}")
    ledger2 = Ledger()
    adapter2, seen2 = voice_with(slow)
    with pytest.raises(NarrationAudioError) as caught:
        build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter2,
                              grant=ledger2.grant, record=ledger2.record)
    assert len(seen2) == 1 and ledger2.evidence[0][1].outcome == "unknown"
    assert ledger2.evidence[0][1].failure_code == "timeout" and KEY not in str(caught.value)


def test_empty_audio_is_billed_invalid():
    adapter, _ = eleven(content=b"")
    ledger = Ledger()
    with pytest.raises(NarrationAudioError):
        build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter,
                              grant=ledger.grant, record=ledger.record)
    assert ledger.evidence[0][1].outcome == "billed_invalid"


def test_narration_script_is_paid_only():
    adapter, seen = eleven()
    free = FreeReport.model_validate(json.loads((FIXTURES / "free_report.json").read_text())["data"])
    ledger = Ledger()
    with pytest.raises(NarrationAudioError):
        build_narration_audio(free, SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter,
                              grant=ledger.grant)
    assert seen == [] and ledger.granted == []


# ---- voice guide --------------------------------------------------------------------------------


def test_guide_prewarm_grants_each_missing_line_once_and_denied_lines_fail_closed():
    adapter, seen = eleven()
    storage, ledger = MemoryStorage(), Ledger(allowed=False)
    guide = VoiceGuide(storage, environ=ELEVEN_ENV, adapter=adapter)
    status = guide.prewarm(grant=ledger.grant, record=ledger.record)
    assert set(status.values()) == {"failed"} and seen == [] and storage.objects == {}
    ledger.allowed = True
    status = guide.prewarm(grant=ledger.grant, record=ledger.record)
    assert set(status.values()) == {"synthesized"} and len(seen) == len(guide.catalog.lines)
    assert all(p.kind == "voice_guide" and p.units.characters <= 2500 for p, _ in ledger.evidence)
    again = len(ledger.granted)
    assert set(guide.prewarm(grant=ledger.grant).values()) == {"cached"} and len(ledger.granted) == again
