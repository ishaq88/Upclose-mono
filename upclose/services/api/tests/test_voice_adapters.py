import io
import json
import wave

import httpx
import pytest

from app.adapters.voice import VoiceProviderError, get_voice_adapter
from app.adapters.voice.__main__ import main
from app.adapters.voice.config import VoiceConfigError, load_env
from app.adapters.voice.elevenlabs import ElevenLabsVoice
from app.adapters.voice.fixture import FixtureVoice

KEY = "sk_test_SECRET123"


def ALLOW(_prepared):
    return True


ENV = {"VOICE_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": KEY, "ELEVENLABS_VOICE_ID": "voiceX",
       "ELEVENLABS_MODEL_ID": "modelY"}


def test_default_is_fixture_silent_wav_synthetic():
    adapter = get_voice_adapter({})
    assert isinstance(adapter, FixtureVoice)
    audio = adapter.synthesize("hi")
    assert audio.provenance == "synthetic" and audio.content_type == "audio/wav"
    with wave.open(io.BytesIO(audio.data)) as w:
        assert w.getnframes() > 0


def test_provider_switch_and_unknown():
    assert isinstance(get_voice_adapter(ENV), ElevenLabsVoice)
    with pytest.raises(VoiceConfigError):
        load_env({"VOICE_PROVIDER": "other"})


@pytest.mark.parametrize("missing", ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID"])
def test_missing_values_fail_closed(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(VoiceConfigError, match=missing):
        load_env(env)


def test_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, content=b"ID3audio")

    audio = get_voice_adapter(ENV, httpx.MockTransport(handler)).synthesize("hello there", grant=ALLOW)
    r = seen["req"]
    assert r.method == "POST"
    assert r.url.path == "/v1/text-to-speech/voiceX" and r.url.host == "api.elevenlabs.io"
    assert r.url.params["output_format"] == "mp3_44100_128"
    assert r.headers["xi-api-key"] == KEY
    assert json.loads(r.content) == {"text": "hello there", "model_id": "modelY"}
    assert audio.data == b"ID3audio" and audio.provenance == "live"


def test_non_200_error_does_not_leak_key_or_text():
    t = httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": f"bad key {KEY}"}))
    with pytest.raises(VoiceProviderError) as e:
        get_voice_adapter(ENV, t).synthesize("secret words", grant=ALLOW)
    assert "401" in str(e.value) and KEY not in str(e.value) and "secret words" not in str(e.value)


def test_transport_error_sanitized():
    def boom(request):
        raise httpx.ConnectError(f"fail {KEY}")

    with pytest.raises(VoiceProviderError) as e:
        get_voice_adapter(ENV, httpx.MockTransport(boom)).synthesize("t", grant=ALLOW)
    assert KEY not in str(e.value) and e.value.__cause__ is None


def test_env_repr_hides_key():
    assert KEY not in repr(load_env(ENV))


def test_cli_fixture(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VOICE_PROVIDER", raising=False)
    src = tmp_path / "n.json"
    src.write_text(json.dumps([{"text": "Score is 7.5"}]))
    assert main([str(src), str(tmp_path / "out"), "--simulated"]) == 0
    assert (tmp_path / "out.wav").read_bytes()[:4] == b"RIFF"
    assert "synthetic" in capsys.readouterr().out


def test_cli_missing_key_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VOICE_PROVIDER", "elevenlabs")
    for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID"):
        monkeypatch.delenv(k, raising=False)
    src = tmp_path / "n.json"
    src.write_text('["a"]')
    assert main([str(src), str(tmp_path / "o")]) == 1
    assert "ELEVENLABS_API_KEY" in capsys.readouterr().err
