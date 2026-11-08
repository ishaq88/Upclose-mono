"""Voice guide: catalog-only ids, cache, auth, key hygiene."""
from __future__ import annotations

import io
import json
import wave

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.voice import VoiceProviderError
from app.adapters.voice.config import VoiceConfigError, load_env
from app.adapters.voice.elevenlabs import ElevenLabsVoice
from app.briefing.voice_guide import (CATALOG_PATH, VoiceGuide, VoiceGuideError, create_voice_guide_router,
                                      load_catalog)
from app.contracts.enums import Zone
from app.services.problems import ProblemError, handle_problem
from tests.test_briefing_common import ELEVEN_ENV, KEY, CountingVoice, MemoryStorage

TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def client(storage=None, environ=None, adapter=None, authenticate=None):
    app = FastAPI()
    app.add_exception_handler(ProblemError, handle_problem)
    app.include_router(create_voice_guide_router(None, storage or MemoryStorage(), environ=environ or {},
                                                 adapter=adapter, authenticate=authenticate))
    return TestClient(app)


def test_catalog_covers_every_zone_engine_sequence_and_warnings():
    catalog = load_catalog()
    ids = {line.id for line in catalog.lines}
    assert {f"zone_{zone.value}" for zone in Zone} <= ids
    assert {"engine_pod_on", "engine_start", "engine_hands_off", "engine_baseline_learned",
            "engine_gentle_rev", "engine_done", "obd_plug_in", "engine_audio", "finish",
            "warn_too_dark", "warn_too_shaky", "warn_too_fast", "warn_car_not_in_frame"} <= ids
    assert all(line.text and line.approx_seconds >= 1 for line in catalog.lines)


def test_client_quality_warning_lines_exist_with_captions():
    catalog = load_catalog()
    for line_id in ("clip_too_short", "clip_too_long", "low_resolution"):
        line = catalog.get(line_id)
        assert line is not None and line.kind == "warning"
        assert line.caption.strip() and line.approx_seconds >= 1
    described = {row["id"]: row for row in VoiceGuide(MemoryStorage(), environ={}).describe()["lines"]}
    assert all(described[i]["caption"] for i in ("clip_too_short", "clip_too_long", "low_resolution"))


def test_invalid_catalogs_are_rejected(tmp_path):
    raw = json.loads(CATALOG_PATH.read_text())
    bad = {**raw, "lines": raw["lines"] + [raw["lines"][0]]}
    (tmp_path / "dup.json").write_text(json.dumps(bad))
    with pytest.raises(VoiceGuideError):
        load_catalog(tmp_path / "dup.json")
    no_zone = {**raw, "lines": [row for row in raw["lines"] if row.get("zone") != "tires"]}
    (tmp_path / "zone.json").write_text(json.dumps(no_zone))
    with pytest.raises(VoiceGuideError):
        load_catalog(tmp_path / "zone.json")
    with pytest.raises(VoiceGuideError):
        load_catalog(tmp_path / "missing.json")


def test_catalog_route_and_auth_required():
    c = client()
    assert c.get("/api/v1/voice-guide").status_code == 401
    assert c.get("/api/v1/voice-guide/finish").status_code == 401
    assert c.get("/api/v1/voice-guide", headers={"Authorization": "Bearer short"}).status_code == 401
    body = c.get("/api/v1/voice-guide", headers=AUTH).json()
    assert body["voice_label"] == "synthesized voice" and body["provenance"] == "synthetic"
    assert len(body["lines"]) == len(load_catalog().lines)
    assert {"id", "caption", "approx_seconds", "kind"} <= set(body["lines"][0])


def test_authenticate_hook_can_reject():
    def deny(token):
        raise ProblemError(401, "TOKEN_INVALID", "Unauthorized", "no")
    c = client(authenticate=deny)
    assert c.get("/api/v1/voice-guide", headers=AUTH).status_code == 401
    assert c.get("/api/v1/voice-guide/finish", headers=AUTH).status_code == 401


def test_only_catalog_ids_are_served_and_never_synthesized():
    voice = CountingVoice()
    c = client(adapter=voice)
    for bad in ("nope", "..%2Fsecret", "zone_", "FINISH", "finish%20", "Hello%20world"):
        assert c.get(f"/api/v1/voice-guide/{bad}", headers=AUTH).status_code == 404
    assert voice.calls == []
    ok = c.get("/api/v1/voice-guide/finish", headers=AUTH)
    assert ok.status_code == 200 and ok.headers["x-voice-provenance"] == "synthetic"
    assert ok.headers["x-voice-label"] == "synthesized voice" and ok.content == b"AUDIO"


def test_fixture_audio_is_a_valid_wav():
    r = client().get("/api/v1/voice-guide/zone_tires", headers=AUTH)
    assert r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getnframes() > 0


def test_cache_hit_avoids_second_synthesis_across_instances():
    voice, storage = CountingVoice(), MemoryStorage()
    c = client(storage, adapter=voice)
    first = c.get("/api/v1/voice-guide/engine_start", headers=AUTH)
    second = c.get("/api/v1/voice-guide/engine_start", headers=AUTH)
    assert first.headers["x-voice-cache"] == "miss" and second.headers["x-voice-cache"] == "hit"
    assert len(voice.calls) == 1 and storage.puts == 1
    client(storage, adapter=voice).get("/api/v1/voice-guide/engine_start", headers=AUTH)
    assert len(voice.calls) == 1  # a new process still hits the stored object
    assert all(key.startswith("voice-guide/") and key.endswith(".wav") for key in storage.objects)


def test_cache_key_depends_on_text_voice_model_provider():
    storage = MemoryStorage()
    fixture = VoiceGuide(storage, environ={})
    eleven = VoiceGuide(storage, environ=ELEVEN_ENV, adapter=CountingVoice())
    other = VoiceGuide(storage, environ={**ELEVEN_ENV, "ELEVENLABS_VOICE_ID": "voiceZ"}, adapter=CountingVoice())
    lines = [fixture.catalog.get("finish"), fixture.catalog.get("engine_start")]
    keys = {g.storage_key(line) for g in (fixture, eleven, other) for line in lines}
    assert len(keys) == 6
    assert eleven.storage_key(lines[0]).endswith(".mp3")


def test_provider_failure_is_502_without_key_and_nothing_cached():
    storage = MemoryStorage()
    failing = CountingVoice(fail=RuntimeError(f"auth failed {KEY}"))
    c = client(storage, environ=ELEVEN_ENV, adapter=failing)
    r = c.get("/api/v1/voice-guide/finish", headers=AUTH)
    assert r.status_code == 502 and KEY not in r.text and storage.objects == {}
    http = client(storage, environ=ELEVEN_ENV, adapter=CountingVoice(fail=VoiceProviderError("voice provider returned HTTP 401")))
    r = http.get("/api/v1/voice-guide/finish", headers=AUTH)
    assert r.status_code == 502 and "HTTP 401" in r.text


def test_elevenlabs_request_shape_and_key_never_in_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["fmt"] = request.url.path, request.url.params.get("output_format")
        seen["key"], seen["body"] = request.headers["xi-api-key"], json.loads(request.content)
        return httpx.Response(200, content=b"ID3mp3")
    adapter = ElevenLabsVoice(load_env(ELEVEN_ENV), httpx.MockTransport(handler))
    c = client(environ=ELEVEN_ENV, adapter=adapter)
    r = c.get("/api/v1/voice-guide/finish", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-voice-provenance"] == "live"
    assert seen["path"] == "/v1/text-to-speech/voiceX" and seen["fmt"] == "mp3_44100_128"
    assert seen["body"]["model_id"] == "modelY" and seen["body"]["text"] == load_catalog().get("finish").text
    assert KEY not in r.text and KEY not in json.dumps(dict(r.headers))


def test_misconfigured_live_provider_fails_at_wiring_naming_only_variables():
    with pytest.raises(VoiceConfigError) as caught:
        create_voice_guide_router(None, MemoryStorage(), environ={"VOICE_PROVIDER": "elevenlabs",
                                                                  "ELEVENLABS_API_KEY": KEY})
    assert KEY not in str(caught.value) and "ELEVENLABS_VOICE_ID" in str(caught.value)
