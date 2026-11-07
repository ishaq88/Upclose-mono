"""Voice adapter environment config. Read only here; app/settings.py is untouched."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

TIMEOUT_SECONDS = 60.0
OUTPUT_FORMAT = "mp3_44100_128"  # documented default on the ElevenLabs convert endpoint
BASE_URL = "https://api.elevenlabs.io"


class VoiceConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceEnv:
    provider: str
    api_key: str = field(default="", repr=False)
    voice_id: str = ""
    model_id: str = ""


def load_env(environ: Mapping[str, str] | None = None) -> VoiceEnv:
    env = os.environ if environ is None else environ
    provider = (env.get("VOICE_PROVIDER") or "fixture").strip().lower()
    if provider == "fixture":
        return VoiceEnv(provider="fixture")
    if provider != "elevenlabs":
        raise VoiceConfigError("VOICE_PROVIDER must be 'fixture' or 'elevenlabs'")
    values = {name: (env.get(name) or "").strip() for name in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise VoiceConfigError("missing required environment variable(s): " + ", ".join(missing))
    return VoiceEnv(provider=provider, api_key=values["ELEVENLABS_API_KEY"],
                    voice_id=values["ELEVENLABS_VOICE_ID"], model_id=values["ELEVENLABS_MODEL_ID"])
