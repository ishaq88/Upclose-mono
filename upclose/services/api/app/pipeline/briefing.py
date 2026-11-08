"""Builds the generated, paid-scope additions of a report revision (advisory, VIN check, vibration series).

Provider calls happen here, outside any database transaction, and never raise: each part falls back to a
labeled "not available" form. The result is frozen with the revision (StoredPaidNarration.briefing), so a
re-render never calls a provider again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.contracts.briefing import (FrozenSeriesPoint, FrozenVibration, FrozenVin, FrozenVinDecode, StoredBriefing)
from app.contracts.sources import ObdSummary, RigTelemetryData, RigTelemetrySummary, SourceRecord
from app.adapters.storage import ObjectStorage, StorageError
from app.briefing.advisory_block import AdvisoryBlock, build_advisory_block
from app.services.auxiliary_providers import LIVE_BUDGET_UNAVAILABLE, live_auxiliary_requested
from app.domain import vin as vin_domain
from app.domain.vibration_series import VibrationSeries, vibration_series as computed_series


def _freeze_series(series: VibrationSeries, source: str) -> FrozenVibration:
    return FrozenVibration(series_source=source, state=series.state, reason=series.reason,  # type: ignore[arg-type]
                           sample_count=series.sample_count, duration_s=series.duration_s,
                           points=tuple(FrozenSeriesPoint(t_s=p.t_s, n_accel=p.n_accel, accel_rms=p.accel_rms,
                                                          sound_mean=p.sound_mean, sound_max=p.sound_max)
                                        for p in series.points))


def build_vibration(run_id: str, rig_data: RigTelemetryData | None, environ: Mapping[str, str] | None = None) -> FrozenVibration | None:
    """Tiger Data rollup when the database has this run, else computed from the frozen samples."""
    if rig_data is None:
        return None
    try:
        from app.adapters.tiger import vibration_series as tiger_series
        result = tiger_series(run_id, environ)
        if result.series is not None and result.series.state == "assessed":
            return _freeze_series(result.series, "tiger")
    except Exception:  # Tiger is optional; any failure falls back to the frozen samples
        logging.warning("Tiger series unavailable; computing from frozen samples")
    try:
        return _freeze_series(computed_series(rig_data), "computed")
    except Exception:
        return None


def build_vin(obd: ObdSummary | None, entered_vin: str | None = None, environ: Mapping[str, str] | None = None) -> FrozenVin | None:
    """Local VIN facts plus a decode; a provider failure leaves the facts and a reason, never raw text."""
    raw = None if obd is None else obd.vin
    if not raw:
        return None
    try:
        facts = vin_domain.summarize_vin(raw)
    except vin_domain.VinError:
        return None
    decode = None
    reason = None
    try:
        from app.adapters.vin import get_vin_adapter
        outcome = get_vin_adapter(environ).decode(facts.vin)
        reason = outcome.reason
        if outcome.decode is not None:
            d = outcome.decode
            decode = FrozenVinDecode(make=d.make, model=d.model, model_year=d.model_year, cylinders=d.cylinders,
                                     displacement_l=d.displacement_l, fuel_type=d.fuel_type, body_class=d.body_class,
                                     source=d.source)
    except Exception:
        reason = "decode unavailable"
    return FrozenVin(vin=facts.vin, check_digit=facts.check, year_candidates=facts.years, manufacturer=facts.manufacturer,
                     decode=decode, decode_reason=None if reason is None else str(reason)[:120],
                     match=vin_domain.compare_vins(entered_vin, facts.vin))


def _advisory_key(session_id: str, summaries, vin_info, env: Mapping[str, str]) -> str:
    material = json.dumps([[row.model_dump(mode="json") for row in summaries], vin_info,
                           env.get("ADVISOR_PROVIDER"), env.get("ADVISOR_META_MODEL"), env.get("ADVISOR_GEMINI_MODEL")],
                          sort_keys=True, separators=(",", ":"), default=str)
    return f"media/{session_id}/advisory/{hashlib.sha256(material.encode()).hexdigest()}.json"


def _stored_advisory(storage: ObjectStorage | None, key: str) -> AdvisoryBlock | None:
    """The first persisted provider answer for these exact inputs; a retry publishes it instead of asking again."""
    if storage is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="upclose-advisory-") as scratch:
            path = Path(scratch) / "block.json"
            if storage.head_size(key) is None:
                return None
            storage.download_to(key, path, 256 * 1024)
            return AdvisoryBlock.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (StorageError, OSError, ValueError):
        return None


def _store_advisory(storage: ObjectStorage | None, key: str, block: AdvisoryBlock) -> None:
    if storage is None:
        return
    try:
        with tempfile.TemporaryDirectory(prefix="upclose-advisory-") as scratch:
            path = Path(scratch) / "block.json"
            path.write_text(json.dumps(block.to_json()), encoding="utf-8")
            storage.put_file(key, path, "application/json")
    except (StorageError, OSError):
        logging.warning("Advisory answer could not be persisted; a retry will not ask the provider again")


def _live_advisory(summaries, vin_info, session_id: str, settings, environ, dispatcher, storage) -> AdvisoryBlock:  # type: ignore[no-untyped-def]
    env = os.environ if environ is None else environ
    provider = (env.get("ADVISOR_PROVIDER") or "unknown").strip().lower()
    provider = provider if provider in ("meta", "gemini") else "unknown"
    unavailable = AdvisoryBlock(status="not_available", provider=provider, model="unavailable", prompt_version="advisory-v1",
                                provenance="live", reason=LIVE_BUDGET_UNAVAILABLE)
    if dispatcher is None or not dispatcher.available:
        return unavailable
    key = _advisory_key(session_id, summaries, vin_info, env)
    stored = _stored_advisory(storage, key)
    if stored is not None:
        return stored
    try:
        block = build_advisory_block(summaries, vin_info, settings, environ=environ, grant=dispatcher.grant,
                                     record=dispatcher.record, deadline_seconds=min(45.0, dispatcher.remaining_seconds()))
    finally:
        dispatcher.close()  # a granted call whose usage was never recorded keeps its unknown charge
    if dispatcher.granted:  # a request may have been billed: keep the first answer, whatever it was
        _store_advisory(storage, key, block)
    return block


def build_briefing(sources: Sequence[SourceRecord], rig_data: RigTelemetryData | None, session_id: str, settings,
                   environ: Mapping[str, str] | None = None, *, dispatcher=None,  # type: ignore[no-untyped-def]
                   storage: ObjectStorage | None = None) -> StoredBriefing:
    obd = next((row.summary for row in sources if isinstance(row.summary, ObdSummary) and row.summary.state == "assessed"), None)
    vin = build_vin(obd, None, environ)
    vin_info = None
    if vin is not None and vin.decode is not None:
        vin_info = {"year": vin.decode.model_year, "make": vin.decode.make, "model": vin.decode.model}
    summaries = tuple(row.summary for row in sources if row.summary.state == "assessed")
    if live_auxiliary_requested("ADVISOR_PROVIDER", environ):
        advisory = _live_advisory(summaries, vin_info, session_id, settings, environ, dispatcher, storage)
    else:
        advisory = build_advisory_block(summaries, vin_info, settings, environ=environ, budget_ok=lambda: False)
    return StoredBriefing(advisory=advisory, vin=vin, vibration=build_vibration(session_id, rig_data, environ))
