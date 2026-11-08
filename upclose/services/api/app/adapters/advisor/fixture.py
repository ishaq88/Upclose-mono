"""Fixture advisory provider: deterministic output built from the input flags. Never opens a socket."""

from __future__ import annotations

from collections.abc import Sequence

from app.contracts.sources import ObdSummary, RigTelemetrySummary, SourceSummary
from app.domain.advisory import (AdvisoryConfig, citable_keys, redact_vins, validate_response)

PROVIDER = "fixture"
MODEL = "fixture-v1"


class FixtureAdvisor:
    """Template advisory over the citable input flags, labeled synthetic."""

    def __init__(self, config: AdvisoryConfig) -> None:
        self._config = config

    def advise(self, summaries: Sequence[SourceSummary], *, grant=None, record=None,
               deadline_seconds: float | None = None) -> dict:
        citable = citable_keys(summaries)
        questions: list[dict] = []
        checks: list[dict] = []
        for summary in summaries:
            if isinstance(summary, ObdSummary):
                if summary.mil_on:
                    questions.append({
                        "text": "Ask the seller when the malfunction indicator lamp came on "
                                "and what diagnosis was done since.",
                        "cites": ["obd_snapshot:mil_on"]})
                for dtc in summary.dtcs:
                    questions.append({
                        "text": f"Ask the seller about fault code {dtc.code} ({dtc.status}) "
                                "and for any repair paperwork covering it.",
                        "cites": [f"obd_snapshot:dtc:{dtc.code}"]})
                if summary.codes_recently_cleared:
                    questions.append({
                        "text": "Ask the seller why the fault memory was cleared recently "
                                "and whether the related repair was completed.",
                        "cites": ["obd_snapshot:codes_recently_cleared"]})
                if summary.incomplete_monitors:
                    names = ", ".join(summary.incomplete_monitors)
                    checks.append({
                        "text": f"Have a mechanic complete a drive cycle and re-check "
                                f"these monitors: {names}.",
                        "cites": [f"obd_snapshot:incomplete_monitor:{name}"
                                  for name in summary.incomplete_monitors]})
                if summary.cold_start:
                    questions.append({
                        "text": "Ask the seller how the car is usually warmed up; "
                                "this reading was taken from a cold engine.",
                        "cites": ["obd_snapshot:cold_start"]})
            elif isinstance(summary, RigTelemetrySummary):
                if summary.state == "not_assessed":
                    questions.append({
                        "text": "Ask for a longer, steadier recording; "
                                "this vibration sample could not be assessed.",
                        "cites": ["rig_telemetry:not_assessed"]})
                    continue
                if summary.irregular_windows:
                    count = len(summary.irregular_windows)
                    checks.append({
                        "text": f"Have a mechanic look for the cause of {count} brief "
                                "vibration events noted in this uncalibrated recording.",
                        "cites": ["rig_telemetry:irregular_windows"]})
                if "rough_vibration" in summary.flags:
                    checks.append({
                        "text": "Have a mechanic check for rough running; the uncalibrated "
                                "recording shows uneven vibration.",
                        "cites": ["rig_telemetry:rough_vibration"]})
                if summary.rpm_estimate is not None:
                    checks.append({
                        "text": f"Have a mechanic compare the idle speed with a calibrated "
                                f"tachometer; this estimate is {summary.rpm_estimate} rpm.",
                        "cites": ["rig_telemetry:rpm_estimate"]})
                if summary.temperature is not None:
                    questions.append({
                        "text": "Ask the seller whether the engine was already warm "
                                "when this recording was made.",
                        "cites": ["rig_telemetry:engine_warm_at_start"]})
        flag_count = sum(1 for summary in summaries for _ in getattr(summary, "flags", ()))
        summary = (f"Simulated, uncalibrated evidence with {flag_count} flagged signal(s). "
                   "This advisory was generated from fixed templates, not a model. "
                   "Ask the seller the questions above and have a mechanic perform "
                   "the checks above before deciding.")
        result = validate_response({"questions_for_seller": questions, "mechanic_checks": checks,
                                    "summary": summary}, citable,
                                   provider=PROVIDER, model=MODEL, config=self._config)
        return {key: (redact_vins(value) if isinstance(value, str) else value)
                for key, value in result.to_dict().items()}
