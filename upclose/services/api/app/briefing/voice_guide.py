"""Spoken capture guidance: a fixed catalog of short lines, synthesized once and cached.

There is no free-text input anywhere: a line id either names a catalog entry or is a 404,
which bounds provider cost and abuse. Audio is cached in object storage under

    voice-guide/{sha256(text, voice, model, provider)}.{ext}

This namespace is not session media, so wire the router with the plain (unfenced) storage.

Provider: `VOICE_PROVIDER=fixture` (default) serves a short generated WAV labeled synthetic;
`VOICE_PROVIDER=elevenlabs` uses the existing ElevenLabs adapter (one non-streaming
text-to-speech call per line, `mp3_44100_128`). Keys come from the environment only.

Live synthesis happens only in `prewarm(grant=..., record=...)`, run by a worker with a dispatch
grant. `audio()` without a grant is cache-only for a live provider: a miss never reaches the
adapter (the API serves captions instead). Fixture synthesizes on demand (free).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.adapters.storage import ObjectStorage, StorageError
from app.adapters.voice import VoiceAdapter, VoiceProviderError, get_voice_adapter
from app.adapters.voice.config import VoiceConfigError, load_env
from app.briefing.dispatch import DispatchError, DispatchGrant, UsageRecorder
from app.contracts.enums import Zone
from app.routers.sessions import owner_bearer
from app.services.problems import ProblemError, not_found

from ._cache import fetch_or_store, input_hash

CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "voice_guide.v1.json"
VERSION = "voice-guide-v1"
_WORDS_PER_SECOND = 2.5


class VoiceGuideError(ValueError):
    pass


@dataclass(frozen=True)
class GuideLine:
    id: str
    kind: str
    text: str
    zone: str | None = None

    @property
    def caption(self) -> str:
        return self.text

    @property
    def approx_seconds(self) -> int:
        """Estimate from word count (about 2.5 words per second); not a measured duration."""
        return max(1, round(len(self.text.split()) / _WORDS_PER_SECOND))


@dataclass(frozen=True)
class GuideCatalog:
    version: str
    voice_label: str
    lines: tuple[GuideLine, ...]

    def get(self, line_id: str) -> GuideLine | None:
        return next((line for line in self.lines if line.id == line_id), None)


def load_catalog(path: str | Path = CATALOG_PATH) -> GuideCatalog:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        lines = tuple(GuideLine(id=str(row["id"]), kind=str(row["kind"]), text=str(row["text"]).strip(),
                                zone=row.get("zone")) for row in raw["lines"])
        catalog = GuideCatalog(version=str(raw["version"]), voice_label=str(raw["voice_label"]), lines=lines)
    except (OSError, ValueError, KeyError, TypeError):
        raise VoiceGuideError("voice guide catalog is not readable") from None
    ids = [line.id for line in catalog.lines]
    if catalog.version != VERSION or len(set(ids)) != len(ids) or any(not line.text for line in lines):
        raise VoiceGuideError("voice guide catalog is invalid")
    missing = {zone.value for zone in Zone} - {line.zone for line in catalog.lines}
    if missing:
        raise VoiceGuideError("voice guide catalog lacks a line for every zone")
    return catalog


def _format_for(provider: str) -> tuple[str, str, str]:
    return ("wav", "audio/wav", "synthetic") if provider == "fixture" else ("mp3", "audio/mpeg", "live")


class VoiceGuide:
    def __init__(self, storage: ObjectStorage, *, catalog: GuideCatalog | None = None,
                 environ: Mapping[str, str] | None = None, adapter: VoiceAdapter | None = None) -> None:
        self.catalog = catalog or load_catalog()
        self._storage = storage
        self._env = load_env(environ)  # raises VoiceConfigError (names variables only) when misconfigured
        self._adapter = adapter or get_voice_adapter(environ)

    @property
    def voice_id(self) -> str:
        return self._env.voice_id or "fixture"

    @property
    def model_id(self) -> str:
        return self._env.model_id or "fixture-v1"

    def storage_key(self, line: GuideLine) -> str:
        extension = _format_for(self._env.provider)[0]
        return f"voice-guide/{input_hash(line.text, self.voice_id, self.model_id, self._env.provider)}.{extension}"

    @property
    def live(self) -> bool:
        return self._env.provider != "fixture"

    def audio(self, line_id: str, *, grant: DispatchGrant | None = None,
              record: UsageRecorder | None = None, deadline_seconds: float | None = None
              ) -> tuple[bytes, str, str, bool]:
        """(bytes, content_type, provenance, cache_hit) for a catalog line; unknown id -> 404.

        Live provider: a hit is served; a miss synthesizes only with a `grant`, else 503 (cache-only)."""
        line = self.catalog.get(line_id)
        if line is None:
            raise not_found()
        extension, content_type, provenance = _format_for(self._env.provider)

        def unavailable(detail: str) -> ProblemError:
            return ProblemError(503 if self.live else 502, "VOICE_UNAVAILABLE", "Voice unavailable", detail)

        def synthesize() -> bytes:
            if self.live and grant is None:
                raise unavailable("Spoken guidance is not available yet; use the written captions.")
            try:
                if self.live:
                    return self._adapter.synthesize(line.text, grant=grant, record=record, kind="voice_guide",
                                                    deadline_seconds=deadline_seconds).data
                return self._adapter.synthesize(line.text).data
            except ProblemError:
                raise
            except (VoiceProviderError, DispatchError) as exc:  # messages exclude the key
                raise unavailable(f"The voice provider could not produce audio ({str(exc)[:80]}).") from None
            except Exception as exc:
                raise unavailable(f"The voice provider could not produce audio ({type(exc).__name__}).") from None

        try:
            data, hit = fetch_or_store(self._storage, self.storage_key(line), content_type, synthesize,
                                       strict=self.live)
        except StorageError:
            raise unavailable("Spoken guidance is not available right now; use the written captions.") from None
        return data, content_type, provenance, hit

    def prewarm(self, *, grant: DispatchGrant | None, record: UsageRecorder | None = None,
                deadline_seconds: float | None = None) -> dict[str, str]:
        """Synthesize every missing catalog line sequentially, one granted call per line.

        Returns {line_id: "cached" | "synthesized" | "failed"}. A denied grant or provider error marks
        that line "failed" and moves on; nothing is retried here."""
        import time
        deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        status: dict[str, str] = {}
        for line in self.catalog.lines:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0.1:
                status[line.id] = "failed"
                continue
            try:
                _, _, _, hit = self.audio(line.id, grant=grant, record=record, deadline_seconds=remaining)
                status[line.id] = "cached" if hit else "synthesized"
            except ProblemError:
                status[line.id] = "failed"
        return status

    def describe(self) -> dict:
        return {"version": self.catalog.version, "voice_label": self.catalog.voice_label,
                "provider": self._env.provider,
                "provenance": _format_for(self._env.provider)[2],
                "lines": [{"id": line.id, "kind": line.kind, "zone": line.zone, "caption": line.caption,
                           "approx_seconds": line.approx_seconds} for line in self.catalog.lines]}


def create_voice_guide_router(settings, storage: ObjectStorage, *,
                              environ: Mapping[str, str] | None = None,
                              adapter: VoiceAdapter | None = None,
                              authenticate: Callable[[str], object] | None = None) -> APIRouter:
    """Owner-bearer routes. `authenticate(token)` may be supplied to verify the token against the
    database (raise ProblemError 401 on failure); without it only the token format is checked."""
    router = APIRouter(prefix="/api/v1")
    guide = VoiceGuide(storage, environ=environ, adapter=adapter)  # VoiceConfigError names variables only

    def check(token: str) -> None:
        if authenticate is not None:
            authenticate(token)

    @router.get("/voice-guide")
    def catalog(token: Annotated[str, Depends(owner_bearer)]) -> Response:
        check(token)
        return Response(json.dumps(guide.describe()), media_type="application/json",
                        headers={"Cache-Control": "private, no-store"})

    @router.get("/voice-guide/{line_id}")
    def line_audio(line_id: str, token: Annotated[str, Depends(owner_bearer)]) -> Response:
        check(token)
        data, content_type, provenance, hit = guide.audio(line_id)
        return Response(data, media_type=content_type,
                        headers={"Cache-Control": "private, max-age=86400",
                                 "X-Voice-Provenance": provenance,
                                 "X-Voice-Label": guide.catalog.voice_label,
                                 "X-Voice-Cache": "hit" if hit else "miss"})

    return router
