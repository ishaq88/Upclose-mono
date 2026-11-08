"""Narration audio: free/paid boundary, cache reuse, metadata, key hygiene."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.adapters.voice import VoiceProviderError
from app.adapters.voice.elevenlabs import ElevenLabsVoice
from app.adapters.voice.config import load_env
from app.briefing.narration_audio import NarrationAudioError, build_narration_audio, narration_key
from app.contracts.reports import FreeReport
from tests.test_briefing_common import (ELEVEN_ENV, KEY, SESSION, CountingVoice, MemoryStorage, allow,
                                        FIXTURES, paid_report)


def test_fixture_mode_audio_metadata_and_key_convention():
    report = paid_report()
    storage = MemoryStorage()
    audio = build_narration_audio(report, SESSION, storage, environ={})
    assert audio.provider == "fixture" and audio.provenance == "synthetic" and audio.label == "synthesized voice"
    assert audio.sha256 == hashlib.sha256(audio.data).hexdigest()
    assert audio.storage_key.startswith(f"media/{SESSION}/narration/{report.revision_id}/")
    assert audio.storage_key.endswith(".wav") and audio.storage_key in storage.objects
    assert narration_key(SESSION, report.revision_id, "abc", "mp3").endswith("/abc.mp3")
    meta = audio.metadata()
    assert "data" not in meta and meta["size_bytes"] == len(audio.data) and meta["voice_id"] == "fixture"


def test_cache_hit_avoids_second_synthesis():
    voice, storage, report = CountingVoice(), MemoryStorage(), paid_report()
    first = build_narration_audio(report, SESSION, storage, environ={}, adapter=voice)
    second = build_narration_audio(report, SESSION, storage, environ={}, adapter=voice)
    assert len(voice.calls) == 1 and not first.cache_hit and second.cache_hit
    assert first.data == second.data and first.storage_key == second.storage_key
    build_narration_audio(paid_report(["A different sentence."]), SESSION, storage, environ={}, adapter=voice)
    assert len(voice.calls) == 2


def test_script_is_only_the_report_narration_plus_disclosure():
    voice = CountingVoice()
    build_narration_audio(paid_report(["First statement.", "Second statement."]), SESSION, None,
                          environ={}, adapter=voice)
    assert voice.calls[0].startswith("First statement. Second statement.")
    assert voice.calls[0].endswith("This audio is AI-generated from your report.")


def test_free_report_is_refused():
    free = json.loads((FIXTURES / "free_report.json").read_text())["data"]
    voice = CountingVoice()
    with pytest.raises(NarrationAudioError):
        build_narration_audio(FreeReport.model_validate(free), SESSION, None, environ={}, adapter=voice)
    with pytest.raises(NarrationAudioError):
        build_narration_audio({"scope": "paid", "narration": [{"text": "x"}]}, SESSION, None,  # type: ignore[arg-type]
                              environ={}, adapter=voice)
    assert voice.calls == []


def test_not_ready_or_empty_report_is_refused():
    voice = CountingVoice()
    for report in (paid_report(state="building"), paid_report(narration=[])):
        with pytest.raises(NarrationAudioError):
            build_narration_audio(report, SESSION, None, environ={}, adapter=voice)
    assert voice.calls == []


def test_provider_error_never_contains_key_or_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.headers['xi-api-key']}")
    adapter = ElevenLabsVoice(load_env(ELEVEN_ENV), httpx.MockTransport(handler))
    with pytest.raises(NarrationAudioError) as caught:
        build_narration_audio(paid_report(["Secret findings text."]), SESSION, MemoryStorage(),
                              environ=ELEVEN_ENV, adapter=adapter, grant=allow)
    assert KEY not in str(caught.value) and "Secret findings" not in str(caught.value)
    boom = CountingVoice(fail=RuntimeError(f"leaked {KEY}"))
    with pytest.raises(NarrationAudioError) as caught:
        build_narration_audio(paid_report(), SESSION, None, environ=ELEVEN_ENV, adapter=boom, grant=allow)
    assert KEY not in str(caught.value)
    http_fail = CountingVoice(fail=VoiceProviderError("voice provider returned HTTP 401"))
    with pytest.raises(NarrationAudioError, match="HTTP 401"):
        build_narration_audio(paid_report(), SESSION, None, environ=ELEVEN_ENV, adapter=http_fail, grant=allow)


def test_elevenlabs_path_is_live_mp3_with_provider_voice_and_model():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/text-to-speech/voiceX"
        return httpx.Response(200, content=b"ID3mp3bytes")
    adapter = ElevenLabsVoice(load_env(ELEVEN_ENV), httpx.MockTransport(handler))
    audio = build_narration_audio(paid_report(), SESSION, MemoryStorage(), environ=ELEVEN_ENV, adapter=adapter,
                                   grant=allow)
    assert (audio.provider, audio.voice_id, audio.model_id) == ("elevenlabs", "voiceX", "modelY")
    assert audio.provenance == "live" and audio.extension == "mp3" and audio.storage_key.endswith(".mp3")
