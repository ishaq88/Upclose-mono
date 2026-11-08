"""Session-owned prewarm of the fixed voice-guide catalog.

The capture guide's audio is a fixed catalog of short lines cached globally in object storage. The API route
only serves that cache. Producing the audio is paid, so it happens here, in the worker, under a real session's
job and lease: every missing line needs a granted, priced reservation (see `AuxiliaryDispatcher`) before any
byte leaves the process. Cached lines cost nothing and need no grant; a line that was already attempted is never
bought twice. The job never fails for a provider or budget problem: the guide then simply shows its captions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.enums import SessionState
from app.jobs.queue import Failure, JobSpec, Lease
from app.jobs.worker import Publication, StageFailure
from app.models import Session
from app.services.auxiliary_budget import AuxiliaryDispatcher
from app.services.auxiliary_providers import live_auxiliary_requested
from app.services.budgets import BudgetService

STAGE = "voice_prewarm"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prewarm_spec(session_id, environ: Mapping[str, str] | None = None) -> JobSpec | None:  # type: ignore[no-untyped-def]
    """The job a new session enqueues, or None when the voice is the free fixture (nothing to pay for)."""
    env = os.environ if environ is None else environ
    if not live_auxiliary_requested("VOICE_PROVIDER", env):
        return None
    if (env.get("VOICE_PREWARM") or "").strip().lower() != "true":
        return None  # opt-in: the guide prewarm buys many lines, so it stays off unless the operator asks
    from app.briefing.voice_guide import CATALOG_PATH
    catalog = hashlib.sha256(Path(CATALOG_PATH).read_bytes()).hexdigest()
    voice = {key: (env.get(key) or "") for key in ("VOICE_PROVIDER", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID")}
    return JobSpec(session_id, "session", str(session_id), STAGE, _digest({"session": str(session_id), "catalog": catalog}),
                   _digest({"catalog": catalog, "voice": voice}))


class VoicePrewarmHandler:
    def __init__(self, factory: sessionmaker[DBSession], budget: BudgetService | None, guide_factory: Callable[[], object]) -> None:
        self.factory, self.budget, self.guide_factory = factory, budget, guide_factory

    def __call__(self, lease: Lease) -> Publication:
        if lease.session_id is None or lease.subject_id != str(lease.session_id):
            raise StageFailure(Failure("INVALID_INPUT", retryable=False))
        with self.factory() as db:
            state = db.scalar(select(Session.state).where(Session.id == lease.session_id))
        if state is None or state in (SessionState.DELETING, SessionState.DELETED):
            return Publication()
        dispatcher = AuxiliaryDispatcher(self.budget, lease)
        if not dispatcher.available:
            logging.warning("Voice guide prewarm skipped: priced budget reservation is not connected for this worker")
            return Publication()
        try:
            guide = self.guide_factory()
            results = guide.prewarm(grant=dispatcher.grant, record=dispatcher.record)  # type: ignore[attr-defined]
            logging.info("Voice guide prewarm finished: %s", _summary(results))
        except Exception as error:  # captions remain; a provider problem must not burn retries or hide a charge
            logging.warning("Voice guide prewarm stopped (%s)", type(error).__name__)
        finally:
            dispatcher.close()
        return Publication()


def _summary(results: object) -> str:
    if isinstance(results, Mapping):
        counts: dict[str, int] = {}
        for value in results.values():
            counts[str(value)] = counts.get(str(value), 0) + 1
        return ", ".join(f"{key}={count}" for key, count in sorted(counts.items()))
    return "ok"


def register_prewarm_handler(registry, factory, budget, guide_factory) -> None:  # type: ignore[no-untyped-def]
    registry.register(STAGE, VoicePrewarmHandler(factory, budget, guide_factory))
