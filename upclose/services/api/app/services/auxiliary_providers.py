"""Live auxiliary generation is reachable only through a priced dispatch grant (see `auxiliary_budget`)."""
import os
from collections.abc import Mapping

from app.adapters.voice import VoiceAudio, VoiceProviderError

LIVE_BUDGET_UNAVAILABLE = "Live generation unavailable: no priced budget grant was available for this call."


def live_auxiliary_requested(variable: str, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(variable) or "fixture").strip().lower() != "fixture"


class CacheOnlyVoice:
    """The API process never holds a provider transport: it serves cached guide audio and nothing else."""

    def synthesize(self, text: str, **_: object) -> VoiceAudio:
        raise VoiceProviderError("budget reservation unavailable; use the written captions")


def guarded_guide_adapter(environ: Mapping[str, str] | None = None) -> CacheOnlyVoice | None:
    return CacheOnlyVoice() if live_auxiliary_requested("VOICE_PROVIDER", environ) else None


UnfundedVoice = CacheOnlyVoice  # earlier name
