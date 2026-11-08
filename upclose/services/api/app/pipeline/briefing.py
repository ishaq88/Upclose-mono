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
from dataclasses import replace
from pathlib import Path

from app.contracts.briefing import (FrozenSeriesPoint, FrozenVibration, FrozenVin, FrozenVinDecode, StoredBriefing)
from app.contracts.sources import ObdSummary, RigTelemetryData, RigTelemetrySummary, SourceRecord
from app.adapters.storage import ObjectStorage, StorageError
from app.briefing.advisory_block import AdvisoryBlock, build_advisory_block
from app.services.auxiliary_providers import LIVE_BUDGET_UNAVAILABLE, live_auxiliary_requested
from app.contracts.enums import PART_ZONES
from app.domain import vin as vin_domain
from app.domain.advisory import PROMPT_VERSION, ContextFinding, ContextGap, ReportContext
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


# Flags the backend sets for its own de-duplication; they never make a finding serious (as in the web report).
_INTERNAL_FLAGS = frozenset({"duplicate_review", "dedup_merged"})


def _words(value: object) -> str:
    return str(getattr(value, "value", value)).replace("_", " ")


def report_context(manifest, owner_label: str | None = None) -> ReportContext:  # type: ignore[no-untyped-def]
    """Confirmed findings and unchecked zones of a frozen manifest, for the advisory.

    Finding numbers follow the web report's order (serious first, then severity, zone order, id), so
    `finding:<n>` is the number the reader sees. No frames, URLs or confidence values are carried.
    """
    verified = [row for part in manifest.parts for row in part.verified]
    verified.sort(key=lambda f: (not any(flag not in _INTERNAL_FLAGS for flag in f.flags), -(f.severity or 0),
                                 PART_ZONES.index(f.zone), str(f.id)))
    findings = tuple(ContextFinding(
        number=number, concept=_words(f.concept), place=_words(f.component_id or f.subzone or f.zone),
        severity=int(f.severity or 0), serious=any(flag not in _INTERNAL_FLAGS for flag in f.flags),
        note=f.evidence_note) for number, f in enumerate(verified, start=1))
    gaps: list[ContextGap] = []
    for part in manifest.parts:
        rows = [row for row in part.coverage if row.state.value != "assessed"]
        if rows:
            state = "not_assessed" if any(row.state.value == "not_assessed" for row in rows) else "partial"
            reasons = list(dict.fromkeys(row.reason for row in rows if row.reason))
            gaps.append(ContextGap(zone=part.zone.value, state=state, reason="; ".join(reasons)[:240] or "no reason recorded"))
        elif not part.coverage and part.assess_run_id is None:
            gaps.append(ContextGap(zone=part.zone.value, state="not_assessed",
                                   reason=(part.failure_reason or f"area {part.state.value}")[:240]))
    return ReportContext(findings=findings, gaps=tuple(gaps), owner_label=(owner_label or "").strip() or None)


def _decoded_identity(vin: FrozenVin | None) -> dict[str, object]:
    if vin is None or vin.decode is None:
        return {}
    d = vin.decode
    fields = {"year": d.model_year, "make": d.make, "model": d.model, "cylinders": d.cylinders,
              "displacement_l": d.displacement_l, "fuel_type": d.fuel_type, "body_class": d.body_class}
    return {key: value for key, value in fields.items() if value is not None}


def _advisory_key(session_id: str, summaries, vin_info, env: Mapping[str, str]) -> str:
    material = json.dumps([[row.to_json() if isinstance(row, ReportContext) else row.model_dump(mode="json")
                            for row in summaries], vin_info, PROMPT_VERSION,
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
    unavailable = AdvisoryBlock(status="not_available", provider=provider, model="unavailable", prompt_version=PROMPT_VERSION,
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
                   storage: ObjectStorage | None = None, context: ReportContext | None = None) -> StoredBriefing:
    obd = next((row.summary for row in sources if isinstance(row.summary, ObdSummary) and row.summary.state == "assessed"), None)
    vin = build_vin(obd, None, environ)
    vin_info = None
    if vin is not None and vin.decode is not None:
        vin_info = {"year": vin.decode.model_year, "make": vin.decode.make, "model": vin.decode.model}
    summaries = tuple(row.summary for row in sources if row.summary.state == "assessed")
    if context is not None:
        context = replace(context, decoded=_decoded_identity(vin))
        if context.has_evidence:  # identity alone never starts an advisory
            summaries = (*summaries, context)
    # With no assessed source, finding or coverage gap, build_advisory_block returns not_available before any provider call.
    if live_auxiliary_requested("ADVISOR_PROVIDER", environ):
        advisory = _live_advisory(summaries, vin_info, session_id, settings, environ, dispatcher, storage)
    else:
        advisory = build_advisory_block(summaries, vin_info, settings, environ=environ, budget_ok=lambda: False)
    return StoredBriefing(advisory=advisory, vin=vin, vibration=build_vibration(session_id, rig_data, environ))
