"""Report voice adapters. `VOICE_PROVIDER=fixture` (default) or `elevenlabs`."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx


class VoiceProviderError(RuntimeError):
    """Provider failure; message never contains the key or the spoken text."""


@dataclass(frozen=True)
class VoiceAudio:
    data: bytes
    content_type: str
    extension: str
    provenance: Literal["synthetic", "live"]


class VoiceAdapter(Protocol):
    def synthesize(self, text: str) -> VoiceAudio: ...


def get_voice_adapter(environ: Mapping[str, str] | None = None,
                      transport: httpx.BaseTransport | None = None) -> VoiceAdapter:
    from .config import load_env
    env = load_env(environ)
    if env.provider == "fixture":
        from .fixture import FixtureVoice
        return FixtureVoice()
    from .elevenlabs import ElevenLabsVoice
    return ElevenLabsVoice(env, transport)
