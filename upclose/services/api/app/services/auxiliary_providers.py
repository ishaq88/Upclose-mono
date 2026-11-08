"""Auxiliary generation cannot dispatch live calls without priced reservation support."""
import os
from collections.abc import Mapping

from app.adapters.voice import VoiceAudio, VoiceProviderError

LIVE_BUDGET_UNAVAILABLE = "Live generation unavailable: priced budget reservation and dispatch are not connected."


def live_auxiliary_requested(variable: str, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(variable) or "fixture").strip().lower() != "fixture"


class UnfundedVoice:
    def synthesize(self, text: str) -> VoiceAudio:
        raise VoiceProviderError("budget reservation unavailable; use the written captions")


def guarded_guide_adapter(environ: Mapping[str, str] | None = None) -> UnfundedVoice | None:
    # Cached audio may still be served; generating fresh paid bytes requires a grant.
    return UnfundedVoice() if live_auxiliary_requested("VOICE_PROVIDER", environ) else None
