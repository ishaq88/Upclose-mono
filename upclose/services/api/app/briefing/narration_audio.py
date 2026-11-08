"""Paid-report narration audio.

Text comes only from `PaidReport.narration` (already validated and frozen with the report),
turned into a spoken script by `app.domain.voice_script`. A free report, or a paid report
that is not READY, is refused, so free views can never reach paid narration (invariant 3).

Object-storage key convention (inside the session media namespace so it is deleted with the
session and passes the media fence):

    media/{session_id}/narration/{revision_id}/{input_sha256}.{ext}

`input_sha256` is sha256 over (script, voice id, model id, provider), so a re-run job reuses
the stored audio instead of paying for a second synthesis. The returned `sha256` is the
hash of the audio bytes themselves.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from app.adapters.storage import ObjectStorage
from app.adapters.voice import VoiceAdapter, VoiceProviderError, get_voice_adapter
from app.adapters.voice.config import load_env
from app.contracts.enums import ReportState
from app.contracts.reports import PaidReport
from app.domain.voice_script import VoiceScriptError, build_script, load_voice_config

from ._cache import fetch_or_store, input_hash

VOICE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "voice.v1.json"
SYNTHESIZED_LABEL = "synthesized voice"


class NarrationAudioError(RuntimeError):
    """Refusal or provider failure; never contains the key or the spoken text."""


@dataclass(frozen=True)
class NarrationAudio:
    data: bytes
    content_type: str
    extension: str
    provider: str
    voice_id: str
    model_id: str
    provenance: Literal["synthetic", "live"]
    sha256: str
    storage_key: str
    cache_hit: bool
    label: str = SYNTHESIZED_LABEL

    def metadata(self) -> dict:
        """Everything except the bytes, for persistence next to the report."""
        return {"provider": self.provider, "voice_id": self.voice_id, "model_id": self.model_id,
                "provenance": self.provenance, "sha256": self.sha256, "storage_key": self.storage_key,
                "content_type": self.content_type, "size_bytes": len(self.data), "label": self.label}


def narration_key(session_id: UUID, revision_id: UUID, digest: str, extension: str) -> str:
    return f"media/{session_id}/narration/{revision_id}/{digest}.{extension}"


def build_narration_audio(report: PaidReport, session_id: UUID, storage: ObjectStorage | None = None, *,
                          environ: Mapping[str, str] | None = None, adapter: VoiceAdapter | None = None,
                          simulated: bool = False) -> NarrationAudio:
    if not isinstance(report, PaidReport) or report.scope != "paid":
        raise NarrationAudioError("narration audio is only built from a paid report")
    if report.state != ReportState.READY:
        raise NarrationAudioError("narration audio needs a ready report")
    if not report.narration:
        raise NarrationAudioError("the report has no narration to speak")
    try:
        script = build_script(report.narration, load_voice_config(VOICE_CONFIG_PATH), simulated=simulated)
        env = load_env(environ)
    except (VoiceScriptError, RuntimeError, OSError, ValueError) as exc:
        raise NarrationAudioError(f"narration script or voice config invalid ({type(exc).__name__})") from None
    voice = adapter or get_voice_adapter(environ)
    provider = env.provider
    voice_id = env.voice_id or "fixture"
    model_id = env.model_id or "fixture-v1"
    holder: list = []

    def synthesize() -> bytes:
        try:
            audio = voice.synthesize(script)
        except VoiceProviderError as exc:  # adapter messages exclude the key and the text
            raise NarrationAudioError(f"voice provider failed: {str(exc)[:120]}") from None
        except Exception as exc:
            raise NarrationAudioError(f"voice provider failed ({type(exc).__name__})") from None
        holder.append(audio)
        return audio.data

    digest = input_hash(script, voice_id, model_id, provider)
    extension, content_type, provenance = _format_for(provider)
    key = narration_key(session_id, report.revision_id, digest, extension)
    if storage is None:
        data, hit = synthesize(), False
    else:
        data, hit = fetch_or_store(storage, key, content_type, synthesize)
    if holder:
        extension, content_type, provenance = holder[0].extension, holder[0].content_type, holder[0].provenance
    return NarrationAudio(data=data, content_type=content_type, extension=extension, provider=provider,
                          voice_id=voice_id, model_id=model_id, provenance=provenance,
                          sha256=hashlib.sha256(data).hexdigest(),
                          storage_key=key if storage is not None else "", cache_hit=hit)


def _format_for(provider: str) -> tuple[str, str, Literal["synthetic", "live"]]:
    """Format the adapter will produce: fixture is a WAV, ElevenLabs is MP3 (mp3_44100_128)."""
    if provider == "fixture":
        return "wav", "audio/wav", "synthetic"
    return "mp3", "audio/mpeg", "live"
