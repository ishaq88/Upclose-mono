"""Validated buyer advisory over frozen evidence-source summaries. Pure: no network, ORM or framework imports.

Given the derived summaries of an inspection's evidence sources (see the contract,
"Derived summaries"), this module builds the model prompt and strictly validates the
untrusted model output into a short advisory: questions for the seller, checks for a
mechanic, and a plain-text summary.

Besides the source summaries, the input may carry a ``ReportContext``: the report's confirmed
visual findings, the zones that were not (fully) checked, and the vehicle identity when it is
actually known (owner-entered label, or decoded from a VIN). The context travels inside the same
sequence as the summaries, so adapters need no new parameter.

Every advisory item must cite at least one evidence key that exists in the input
(for example ``obd_snapshot:dtc:P0301``, ``rig_telemetry:irregular_windows``, ``finding:2`` or
``coverage:underbody``). Items with missing or foreign citations are dropped. When nothing valid survives, the
result is ``not_available`` with a reason -- never an empty "all clear".
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts.sources import ObdSummary, RigTelemetrySummary, SourceSummary

PROMPT_VERSION = "advisory-v2"

FALLBACK_SUMMARY = ("Ask the seller the questions above and have a mechanic perform "
                    "the checks above; the underlying readings are uncalibrated estimates.")

_ALL_CLEAR = re.compile(r"\ball\s*clear\b|\bno\s+(concerns|issues?|problems?|faults?)\b", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]*>")
_MARKDOWN_LINK = re.compile(r"!\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_FMT = re.compile(r"(\*\*|__|\*|_|`|#+)")
_VIN_LIKE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class AdvisoryConfig:
    prompt_version: str
    max_items_per_list: int
    max_item_chars: int
    max_summary_chars: int
    timeout_seconds: float = 30.0
    max_attempts: int = 2

    def __post_init__(self) -> None:
        if self.prompt_version != PROMPT_VERSION:
            raise AdvisoryError(f"advisory config version must be {PROMPT_VERSION!r}")
        if self.max_items_per_list < 1 or self.max_item_chars < 1 or self.max_summary_chars < 1:
            raise AdvisoryError("advisory limits must be positive")
        if not 0 < self.timeout_seconds <= 30.0:
            raise AdvisoryError("advisory timeout must be within (0, 30] seconds")
        if self.max_attempts not in (1, 2):
            raise AdvisoryError("advisory max_attempts must be 1 or 2 (one call plus at most one retry)")


def load_advisory_config(path: str | Path) -> AdvisoryConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdvisoryError("advisory config is not readable JSON") from exc
    if not isinstance(raw, dict):
        raise AdvisoryError("advisory config must be a JSON object")
    try:
        return AdvisoryConfig(prompt_version=str(raw["version"]),
                              max_items_per_list=int(raw["max_items_per_list"]),
                              max_item_chars=int(raw["max_item_chars"]),
                              max_summary_chars=int(raw["max_summary_chars"]),
                              timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
                              max_attempts=int(raw.get("max_attempts", 2)))
    except (KeyError, TypeError, ValueError) as exc:
        raise AdvisoryError("advisory config is missing required limits") from exc


class AdvisoryError(ValueError):
    """Config or input problem; message never contains keys, VINs, or payloads."""


@dataclass(frozen=True)
class AdvisoryItem:
    text: str
    cites: tuple[str, ...]


@dataclass(frozen=True)
class Advisory:
    questions_for_seller: tuple[AdvisoryItem, ...]
    mechanic_checks: tuple[AdvisoryItem, ...]
    summary: str
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict:
        def item(row: AdvisoryItem) -> dict:
            return {"text": row.text, "cites": list(row.cites)}
        return {"questions_for_seller": [item(row) for row in self.questions_for_seller],
                "mechanic_checks": [item(row) for row in self.mechanic_checks],
                "summary": self.summary, "provider": self.provider, "model": self.model,
                "prompt_version": self.prompt_version}


@dataclass(frozen=True)
class NotAvailable:
    reason: str
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict:
        return {"status": "not_available", "reason": self.reason, "provider": self.provider,
                "model": self.model, "prompt_version": self.prompt_version}


# ---- report context (visual findings, gaps, vehicle identity) ---------------------------------------


@dataclass(frozen=True)
class ContextFinding:
    """A confirmed finding. ``number`` is its display number in report order (serious first, then
    severity, zone order, finding id), so ``finding:<number>`` matches the number the report shows."""
    number: int
    concept: str
    place: str
    severity: int
    serious: bool
    note: str


@dataclass(frozen=True)
class ContextGap:
    """A zone that was not assessed or only partly checked, with the recorded reason."""
    zone: str
    state: str
    reason: str


@dataclass(frozen=True)
class ReportContext:
    findings: tuple[ContextFinding, ...] = ()
    gaps: tuple[ContextGap, ...] = ()
    owner_label: str | None = None  # owner-entered, unverified
    decoded: Mapping[str, object] = field(default_factory=dict)  # decoded from a VIN (year, make, model, ...)

    @property
    def has_evidence(self) -> bool:
        return bool(self.findings or self.gaps)

    def to_json(self) -> dict:
        return {"findings": [[f.number, f.concept, f.place, f.severity, f.serious, f.note] for f in self.findings],
                "gaps": [[g.zone, g.state, g.reason] for g in self.gaps],
                "owner_label": self.owner_label, "decoded": dict(self.decoded)}


def split_context(items: Sequence[object]) -> tuple[tuple[SourceSummary, ...], ReportContext | None]:
    """Separate the source summaries from the (at most one) report context carried with them."""
    context = next((row for row in items if isinstance(row, ReportContext)), None)
    return tuple(row for row in items if not isinstance(row, ReportContext)), context  # type: ignore[arg-type]


# ---- evidence keys --------------------------------------------------------------------------------


def citable_keys(summaries: Sequence[SourceSummary | ReportContext]) -> frozenset[str]:
    """Every evidence key the input supports as an advisory citation."""
    keys: set[str] = set()
    for summary in summaries:
        if isinstance(summary, ReportContext):
            keys.update(f"finding:{row.number}" for row in summary.findings)
            keys.update(f"coverage:{row.zone}" for row in summary.gaps)
        elif isinstance(summary, RigTelemetrySummary):
            if summary.state == "not_assessed":
                keys.add("rig_telemetry:not_assessed")
                continue
            if summary.dominant_hz is not None:
                keys.add("rig_telemetry:dominant_hz")
            if summary.rpm_estimate is not None:
                keys.add("rig_telemetry:rpm_estimate")
            if summary.roughness is not None:
                keys.add("rig_telemetry:roughness")
            if summary.irregular_windows:
                keys.add("rig_telemetry:irregular_windows")
            if summary.temperature is not None:
                keys.add("rig_telemetry:engine_warm_at_start")
                if summary.temperature.rise_c is not None:
                    keys.add("rig_telemetry:temperature_rise")
            for flag in summary.flags:
                keys.add(f"rig_telemetry:{flag}")
        elif isinstance(summary, ObdSummary):
            if summary.mil_on:
                keys.add("obd_snapshot:mil_on")
            for dtc in summary.dtcs:
                keys.add(f"obd_snapshot:dtc:{dtc.code}")
            for name in summary.incomplete_monitors:
                keys.add(f"obd_snapshot:incomplete_monitor:{name}")
            if summary.codes_recently_cleared:
                keys.add("obd_snapshot:codes_recently_cleared")
            if summary.cold_start:
                keys.add("obd_snapshot:cold_start")
            if summary.coolant_c is not None:
                keys.add("obd_snapshot:coolant_c")
            for flag in summary.flags:
                keys.add(f"obd_snapshot:{flag}")
    return frozenset(keys)


# ---- prompt ---------------------------------------------------------------------------------------


def _evidence_lines(summaries: Sequence[SourceSummary]) -> list[str]:
    lines: list[str] = []
    for summary in summaries:
        if isinstance(summary, RigTelemetrySummary):
            if summary.state == "not_assessed":
                lines.append(f"- rig_telemetry ({summary.provenance}): not assessed; "
                             f"{summary.reason or 'no reason recorded'}")
                continue
            parts = [f"state=assessed", f"provenance={summary.provenance}"]
            if summary.rpm_estimate is not None:
                parts.append(f"rpm_estimate={summary.rpm_estimate} (cylinders={summary.cylinders})")
            if summary.dominant_hz is not None:
                parts.append(f"dominant_hz={summary.dominant_hz}")
            if summary.roughness is not None:
                parts.append(f"roughness={summary.roughness}")
            if summary.irregular_windows:
                stamps = ",".join(str(w.t_ms) for w in summary.irregular_windows)
                parts.append(f"irregular_windows_t_ms=[{stamps}] (unverified vibration events)")
            if summary.temperature is not None:
                parts.append(f"engine_warm_at_start={summary.temperature.engine_warm_at_start} "
                             f"(start_c={summary.temperature.start_c})")
            if summary.flags:
                parts.append(f"flags=[{','.join(summary.flags)}]")
            lines.append("- rig_telemetry: " + "; ".join(parts))
        elif isinstance(summary, ObdSummary):
            parts = [f"provenance={summary.provenance}", f"mil_on={summary.mil_on}"]
            if summary.dtcs:
                codes = ",".join(f"{d.code}/{d.status}" for d in summary.dtcs)
                parts.append(f"dtcs=[{codes}]")
            if summary.incomplete_monitors:
                parts.append(f"incomplete_monitors=[{','.join(summary.incomplete_monitors)}]")
            if summary.codes_recently_cleared is not None:
                parts.append(f"codes_recently_cleared={summary.codes_recently_cleared}")
            if summary.cold_start is not None:
                parts.append(f"cold_start={summary.cold_start}")
            if summary.coolant_c is not None:
                parts.append(f"coolant_c={summary.coolant_c}")
            parts.append("vin_present=" + ("true" if summary.vin else "false"))
            if summary.flags:
                parts.append(f"flags=[{','.join(summary.flags)}]")
            lines.append("- obd_snapshot: " + "; ".join(parts))
    return lines


def _clean(value: object, limit: int) -> str:
    """Untrusted text for the prompt: markup and VIN-like tokens removed, one line, bounded."""
    return redact_vins(plain_text(str(value)))[:limit]


def _quoted(value: object, limit: int) -> str:
    return json.dumps(_clean(value, limit), ensure_ascii=True)


def _context_lines(context: ReportContext) -> tuple[list[str], list[str], list[str]]:
    findings = [f"- finding:{row.number}: {_clean(row.concept.replace("_", " "), 40)} at {_clean(row.place.replace("_", " "), 60)}; "
                f"severity {row.severity} of 5 (uncalibrated); serious={'true' if row.serious else 'false'}; "
                f"note {_quoted(row.note, 240)}" for row in context.findings]
    gaps = [f"- coverage:{row.zone}: {_clean(row.state.replace("_", " "), 20)}; reason {_quoted(row.reason, 240)}"
            for row in context.gaps]
    identity: list[str] = []
    if context.owner_label:
        identity.append(f"- owner-provided, unverified: {_quoted(context.owner_label, 80)}")
    decoded = ", ".join(f"{key}={_clean(value, 64)}" for key, value in context.decoded.items()
                        if value not in (None, ""))
    if decoded:
        identity.append(f"- decoded from the VIN by a public decoder: {decoded}")
    return findings, gaps, identity


def build_prompt(summaries: Sequence[SourceSummary | ReportContext], config: AdvisoryConfig) -> str:
    """Render the validated prompt for the advisory model. Never contains a raw VIN."""
    sources, context = split_context(summaries)
    findings, gaps, identity = _context_lines(context) if context is not None else ([], [], [])
    evidence = "\n".join(_evidence_lines(sources)) or "- (no evidence sources attached)"
    nl = "\n"
    vehicle = (nl.join(identity) if identity else
               "- not known. Do not guess the make, model or year; do not infer them from anything below")
    return (
        "You are helping a used-car buyer. The evidence below may be simulated bench data, "
        "and every value is an uncalibrated estimate, not a measurement. "
        "Do not diagnose with certainty; phrase everything as questions for the seller "
        "and checks for a qualified mechanic.\n"
        f"Evidence:\n{evidence}\n"
        "Confirmed visual findings (from the inspection video; numbered as in the report):\n"
        f"{nl.join(findings) or '- (none)'}\n"
        f"What was not checked or only partly checked:\n{nl.join(gaps) or '- (none)'}\n"
        "Vehicle identity (use it only to make questions and checks specific to this vehicle; "
        f"owner-provided identity is unverified):\n{vehicle}\n"
        "Respond with ONLY a JSON object of this shape:\n"
        '{"questions_for_seller": [{"text": "...", "cites": ["<evidence key>"]}], '
        '"mechanic_checks": [{"text": "...", "cites": ["<evidence key>"]}], '
        '"summary": "..."}\n'
        f"Rules: at most {config.max_items_per_list} items per list; "
        f"each item at most {config.max_item_chars} characters; "
        f"summary at most {config.max_summary_chars} characters of plain text. "
        "Write specific questions for the seller and specific checks for a mechanic, each tied to a "
        "cited finding or to a zone that was not checked, and to the vehicle when its identity is given. "
        "Every item must cite at least one evidence key naming a fact above "
        "(for example obd_snapshot:dtc:P0301, rig_telemetry:irregular_windows, finding:2 or "
        "coverage:underbody). "
        "Cite only facts shown above. Plain text only: no markup, no links, no HTML. "
        "Never claim the vehicle is problem-free."
    )


# ---- validation -----------------------------------------------------------------------------------


def redact_vins(text: str) -> str:
    """Replace VIN-like tokens so errors and logs never carry a VIN."""
    return _VIN_LIKE.sub("<vin-redacted>", text)


def plain_text(value: object) -> str:
    """Strip markup from untrusted model text and collapse whitespace."""
    if not isinstance(value, str):
        return ""
    out = _HTML_TAG.sub("", value)
    out = _MARKDOWN_LINK.sub(lambda m: m.group(1) or m.group(2) or "", out)
    out = _MARKDOWN_FMT.sub("", out)
    return _WHITESPACE.sub(" ", out).strip()


def _clean_item(raw: object, citable: frozenset[str], max_chars: int) -> AdvisoryItem | None:
    if not isinstance(raw, dict):
        return None
    text = plain_text(raw.get("text"))
    if not text:
        return None
    cites = raw.get("cites")
    if not isinstance(cites, list) or not cites:
        return None
    kept: list[str] = []
    for cite in cites:
        if not isinstance(cite, str) or cite not in citable or cite in kept:
            if not isinstance(cite, str) or cite not in citable:
                return None  # foreign or malformed citation rejects the whole item
            continue
        kept.append(cite)
    if not kept:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return AdvisoryItem(text=text, cites=tuple(kept))


def validate_response(payload: object, citable: frozenset[str], *, provider: str, model: str,
                      config: AdvisoryConfig) -> Advisory | NotAvailable:
    """Strictly validate untrusted model output. Invalid items are dropped; an empty
    advisory becomes not_available, never an "all clear"."""
    if not isinstance(payload, dict):
        return NotAvailable(reason="model output was not a JSON object", provider=provider, model=model)
    questions_raw = payload.get("questions_for_seller")
    checks_raw = payload.get("mechanic_checks")
    if not isinstance(questions_raw, list) or not isinstance(checks_raw, list):
        return NotAvailable(reason="model output is missing required lists", provider=provider, model=model)
    questions = tuple(row for raw in questions_raw[:config.max_items_per_list]
                      if (row := _clean_item(raw, citable, config.max_item_chars)) is not None)
    checks = tuple(row for raw in checks_raw[:config.max_items_per_list]
                   if (row := _clean_item(raw, citable, config.max_item_chars)) is not None)
    if not questions and not checks:
        return NotAvailable(reason="no advisory item cited evidence present in the input",
                            provider=provider, model=model)
    summary = plain_text(payload.get("summary"))
    if not summary or _ALL_CLEAR.search(summary):
        summary = FALLBACK_SUMMARY
    if len(summary) > config.max_summary_chars:
        summary = summary[:config.max_summary_chars].rstrip()
    return Advisory(questions_for_seller=questions, mechanic_checks=checks, summary=summary,
                    provider=provider, model=model)


def parse_response_text(raw: str, citable: frozenset[str], *, provider: str, model: str,
                        config: AdvisoryConfig) -> Advisory | NotAvailable:
    """Parse raw model text (tolerating a wrapping code fence) then validate."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except ValueError:
        return NotAvailable(reason="model output was not valid JSON", provider=provider, model=model)
    return validate_response(payload, citable, provider=provider, model=model, config=config)
