"""Report voice adapters. `VOICE_PROVIDER=fixture` (default) or `elevenlabs`.

A live provider makes exactly one transport attempt per prepared request and only behind a
dispatch grant (see app.briefing.dispatch). Fixture never opens a socket.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.briefing.dispatch import DispatchError, DispatchGrant, UsageRecorder


class VoiceProviderError(DispatchError):
    """Provider failure; message never contains the key or the spoken text."""

    def __init__(self, message: str, *, code: str = "provider_error", http_status: int | None = None) -> None:
        super().__init__(message, code=code, http_status=http_status)


@dataclass(frozen=True)
class VoiceAudio:
    data: bytes
    content_type: str
    extension: str
    provenance: Literal["synthetic", "live"]


class VoiceAdapter(Protocol):
    def synthesize(self, text: str, *, grant: DispatchGrant | None = None, record: UsageRecorder | None = None,
                   kind: Literal["narration", "voice_guide"] = "narration",
                   deadline_seconds: float | None = None) -> VoiceAudio: ...


def get_voice_adapter(environ: Mapping[str, str] | None = None,
                      transport: httpx.BaseTransport | None = None) -> VoiceAdapter:
    from .config import load_env
    env = load_env(environ)
    if env.provider == "fixture":
        from .fixture import FixtureVoice
        return FixtureVoice()
    from .elevenlabs import ElevenLabsVoice
    return ElevenLabsVoice(env, transport)
