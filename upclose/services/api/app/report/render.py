"""Pure HTML renderer for frozen report DTOs.

`render_report` takes the scope-filtered DTO produced by the (future) single assembler and
returns a complete HTML document. It never sees storage keys, raw media, entitlement state
or a database session, and it only renders what the DTO contains: the free template cannot
reference paid data because `FreeReport` has no such fields. No JavaScript, no external
assets, no inline `style=` attributes (so the CSP can allow the one static style block by
hash). Output is deterministic for a given DTO and arguments.
"""

import base64
import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from app.contracts.enums import PART_ZONES, ComponentId, CoverageState, ReportState, Zone
from app.contracts.reports import FreeReport, PaidReport, PublicFinding, ReportStatus
from app.contracts.sources import EngineAudioSummary, FreeSourceCheck, ObdSummary, PaidSourceCheck, RigTelemetrySummary, SourceRecord
from app.domain.dedup import INTERNAL_FLAGS

from .anchors import ANCHOR_VERSION, VIEW_HEIGHT, VIEW_LABELS, VIEW_WIDTH, VIEWS, anchor_for

__all__ = ["REPORT_CSP", "REPORT_HEADERS", "PIN_ANCHOR_VERSION", "build_environment", "render_report"]

PIN_ANCHOR_VERSION = ANCHOR_VERSION
NO_FINDINGS_PHRASE = "No visible findings detected in assessed footage"

_APP_DIR = Path(__file__).resolve().parents[1]
_TEMPLATE_DIR = _APP_DIR / "templates" / "report"
_CSS = (_APP_DIR / "static" / "report" / "report.css").read_text(encoding="utf-8").strip()
_CSS_HASH = base64.b64encode(hashlib.sha256(_CSS.encode("utf-8")).digest()).decode("ascii")

# The only inline style is the static block above; no style attributes or scripts exist.
REPORT_CSP = (
    "default-src 'none'; "
    f"style-src 'sha256-{_CSS_HASH}'; "
    "img-src 'self'; media-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
REPORT_HEADERS: dict[str, str] = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": REPORT_CSP,
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

EvidenceUrl = Callable[[UUID], str | None]

_SAFE_PATH = re.compile(r"^/(?!/)[A-Za-z0-9\-._~%:@!$&()*+,;=?/]*$")
_CODE = re.compile(r"^[a-z0-9_]+$")

_STATE_LABELS = {
    CoverageState.ASSESSED: "Assessed",
    CoverageState.PARTIAL: "Partially assessed",
    CoverageState.NOT_ASSESSED: "Not assessed",
}
_VERDICT_LABELS = {
    "supported": "Supported by the assessed footage",
    "contradicted": "Contradicted by the assessed footage",
    "not_visible": "Not visible in the assessed footage",
}
_COMPONENT_LABELS = {
    ComponentId.TIRE_FL: "Front-left tire",
    ComponentId.TIRE_FR: "Front-right tire",
    ComponentId.TIRE_RL: "Rear-left tire",
    ComponentId.TIRE_RR: "Rear-right tire",
}


def build_environment() -> Environment:
    """Autoescaping, strict-undefined environment reading only this package's templates."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR), followlinks=False),
        autoescape=True,
        undefined=StrictUndefined,
        auto_reload=False,
        keep_trailing_newline=True,
    )


_ENV = build_environment()


@dataclass(frozen=True)
class _Card:
    number: int
    concept_label: str
    zone_label: str
    location_label: str
    severity: int
    flags: tuple[str, ...]
    high_risk: bool
    evidence_note: str
    confidence_text: str
    image_src: str | None
    source_time: str | None


def _words(value: str) -> str:
    return value.replace("_", " ")


def _humanize(value: str) -> str:
    """Turn machine codes (part_skipped) into text; leave real sentences alone."""
    if value == "video_zone_unverified":
        return "Section marked by the uploader; area coverage has not been verified."
    if value == "video_zone_unconfirmed":
        return "This area was not identified in the uploaded video."
    if _CODE.match(value):
        text = _words(value)
        return text[:1].upper() + text[1:]
    return value


def _zone_label(zone: Zone) -> str:
    return {Zone.DASH_ELECTRICAL: "Dash and electrical", Zone.FRONT_REAR_BODY: "Front and rear body"}.get(
        zone, _words(zone.value).capitalize()
    )


def _score_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _money(cents: int) -> str:
    dollars, rem = divmod(cents, 100)
    return f"${dollars:,}" if rem == 0 else f"${dollars:,}.{rem:02d}"


def _safe_path(value: str | None) -> str | None:
    return value if value is not None and _SAFE_PATH.match(value) else None


def _location(finding: PublicFinding) -> str:
    if finding.component_id is not None:
        return _COMPONENT_LABELS.get(finding.component_id, _words(finding.component_id.value).capitalize())
    if finding.subzone is not None:
        return _words(finding.subzone.value).capitalize()
    return _zone_label(finding.zone)


def _display_flags(finding: PublicFinding) -> tuple[str, ...]:
    return tuple(_humanize(flag) for flag in sorted(finding.flags) if flag not in INTERNAL_FLAGS)


def _ordered(findings: Sequence[PublicFinding]) -> list[PublicFinding]:
    """Stable card order: part number (zone order), then finding ID."""
    return sorted(findings, key=lambda row: (PART_ZONES.index(row.zone), str(row.id)))


def _cards(findings: Sequence[PublicFinding], evidence_url: EvidenceUrl | None) -> tuple[list[_Card], dict[UUID, int]]:
    cards: list[_Card] = []
    numbers: dict[UUID, int] = {}
    for number, finding in enumerate(_ordered(findings), start=1):
        numbers[finding.id] = number
        src = _safe_path(evidence_url(finding.id)) if evidence_url is not None and finding.evidence_available else None
        flags = _display_flags(finding)
        confidence = "Confidence not available" if finding.confidence is None else f"Model confidence {finding.confidence:.2f}"
        cards.append(_Card(
            number=number,
            concept_label=_words(finding.concept.value).capitalize(),
            zone_label=_zone_label(finding.zone),
            location_label=_location(finding),
            severity=finding.severity,
            flags=flags,
            high_risk=bool(flags),
            evidence_note=finding.evidence_note,
            confidence_text=f"{confidence} ({finding.confidence_label})",
            image_src=src,
            source_time=source_time_label(finding.source_timestamp_ms),
        ))
    return cards, numbers


def source_time_label(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    minutes, remainder = divmod(timestamp_ms, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{minutes}:{seconds:02d}.{milliseconds:03d}"


def _diagram_views(findings: Sequence[PublicFinding], numbers: dict[UUID, int]) -> list[dict[str, object]]:
    groups: dict[tuple[str, float, float], list[PublicFinding]] = defaultdict(list)
    for finding in _ordered(findings):
        anchor = anchor_for(finding.zone, finding.subzone, finding.component_id)
        groups[(anchor.view, anchor.x, anchor.y)].append(finding)
    pins: dict[str, list[dict[str, object]]] = {view: [] for view in VIEWS}
    for (view, x, y), group in groups.items():
        for index, finding in enumerate(group):
            offset = (index - (len(group) - 1) / 2) * 17
            pins[view].append({
                "number": numbers[finding.id],
                "x": f"{x + offset:g}",
                "y": f"{y:g}",
                "severity": finding.severity,
                "title": f"{_words(finding.concept.value)}, severity {finding.severity} of 5",
            })
    return [{"id": view, "label": VIEW_LABELS[view], "pins": sorted(pins[view], key=lambda pin: pin["number"])}
            for view in VIEWS]


def _coverage_rows(report: FreeReport | PaidReport) -> list[dict[str, str]]:
    by_zone = {row.zone: row for row in report.coverage}
    rows = []
    for zone in PART_ZONES:
        row = by_zone.get(zone)
        if row is None:
            continue
        rows.append({
            "zone_label": _zone_label(zone),
            "state": row.state.value,
            "state_label": _STATE_LABELS[row.state],
            "reason": _humanize(row.reason) if row.reason else "—",
        })
    return rows


def _state_notice(state: ReportState) -> str | None:
    if state == ReportState.BUILDING:
        return "This report is still being prepared. What is shown may be incomplete."
    if state == ReportState.FAILED:
        return "This report could not be completed. Nothing here is a full assessment."
    return None


_SOURCE_LABELS = {"engine_audio": "Engine sound (audio clip)", "rig_telemetry": "Vibration and temperature (capture rig)",
                  "obd_snapshot": "Engine computer (OBD) snapshot"}
_PROVENANCE_BADGES = {"synthetic": "Simulated bench data", "cached": "Replayed recording", "live": "Live"}
_FLAG_LABELS = {"irregular_vibration": "Irregular vibration", "rough_vibration": "Rough vibration",
                "mil_on": "Check-engine light on", "trouble_codes": "Trouble codes present",
                "codes_recently_cleared": "Codes may have been recently cleared",
                "anomaly_suspected": "Engine anomaly suspected (relative to its own baseline)"}
_FEATURE_WORDS = {
    "vib_log_frac_fire": "vibration energy at the firing rate", "vib_log_frac_2x": "vibration energy at twice the firing rate",
    "vib_log_frac_half": "vibration energy at half the firing rate", "vib_log_frac_sub": "low-frequency vibration energy",
    "vib_log_frac_hi": "high-frequency vibration energy", "vib_cycle_cv": "cycle-to-cycle irregularity of the vibration",
    "vib_crest": "sharp vibration peaks (crest factor)", "vib_kurtosis": "spikiness of the vibration",
    "vib_log_rms": "overall vibration level", "vib_sub_ratio": "low-frequency to firing-rate energy ratio",
    "aud_env_cycle_cv": "cycle-to-cycle irregularity of the sound", "aud_flux": "how fast the sound spectrum changes"}
MODEL_NOTE = ("A relative signal against this engine's own baseline recording; it is not a diagnosis and not a "
              "calibrated accuracy.")


def _yes_no(value: bool | None) -> str:
    return "Not available" if value is None else "Yes" if value else "No"


def _flag_text(flags: Sequence[str]) -> str:
    return ", ".join(_FLAG_LABELS.get(flag, _words(flag).capitalize()) for flag in flags) or "None"


def _verdict_rows(summary: RigTelemetrySummary) -> list[tuple[str, str]]:
    verdict = summary.model_verdict
    if verdict is None:
        return []
    title = "Engine anomaly model"
    if verdict.verdict == "not_assessed":
        return [(title, f"Not assessed ({_words(verdict.reason or 'no reason given')}). This is not a pass.")]
    rows = [(title, "Anomaly suspected" if verdict.verdict == "anomaly_suspected"
             else "No anomaly detected against the baseline"),
            ("Baseline", f"{verdict.model.baseline_seconds:g} s of this engine's own recording")]
    if verdict.verdict == "anomaly_suspected":
        ranges = "; ".join(f"unlike this engine's own baseline between {row.t_start_s:g}\u2013{row.t_end_s:g} s"
                           for row in verdict.anomalous_runs)
        rows.append(("Anomalous time ranges", ranges or "not located"))
        features = [row.name for row in verdict.top_features]
        rows.append(("Main contributing features", ", ".join(_FEATURE_WORDS.get(name, _words(name)) for name in features) or "not reported"))
    rows.append(("About this signal", MODEL_NOTE))
    return rows


def _free_check_rows(checks: Sequence[FreeSourceCheck]) -> list[dict[str, str]]:
    return [{"label": _SOURCE_LABELS[row.kind],
             "status": "Checked" if row.checked else "Not checked",
             "flags": (f"{row.flag_count} flag{'s' if row.flag_count != 1 else ''} raised" if row.checked or row.flag_count else "\u2014")}
            for row in checks]


def _rig_details(summary: RigTelemetrySummary) -> tuple[list[tuple[str, str]], list[str]]:
    rows: list[tuple[str, str]] = [("Samples", f"{summary.sample_count} over {summary.duration_s:g} s")]
    windows: list[str] = []
    if summary.state == "assessed":
        rows.append(("Dominant vibration frequency", f"{summary.dominant_hz:g} Hz"))
        rows.append(("Engine speed (estimate)",
                     f"about {summary.rpm_estimate} rpm, assuming {summary.cylinders} cylinders"))
        if summary.roughness is not None:
            rows.append(("Roughness (variation of short-window vibration)", f"{summary.roughness:.2f}"))
        windows = [f"{source_time_label(row.t_ms)}: vibration level {row.rms_mg:g} mg, unverified"
                   for row in summary.irregular_windows]
    if summary.temperature is not None:
        rows.append(("Temperature at start", f"{summary.temperature.start_c:g} \u00b0C "
                     f"({'warm' if summary.temperature.engine_warm_at_start else 'cold'} start)"))
        if summary.temperature.rise_c is not None:
            rows.append(("Temperature rise", f"{summary.temperature.rise_c:+g} \u00b0C"))
    rows.extend(_verdict_rows(summary))
    rows.append(("Flags", _flag_text(summary.flags)))
    return rows, windows


def _obd_details(summary: ObdSummary) -> tuple[list[tuple[str, str]], list[str]]:
    rows: list[tuple[str, str]] = [("Check-engine light", "On" if summary.mil_on else "Off"),
        ("Trouble codes", ", ".join(f"{row.code} ({row.status})" for row in summary.dtcs) or "None reported"),
        ("Monitors not yet complete", ", ".join(_words(name) for name in summary.incomplete_monitors) or "None"),
        ("Codes recently cleared", _yes_no(summary.codes_recently_cleared))]
    if summary.distance_since_codes_cleared_km is not None:
        rows.append(("Distance since codes cleared", f"{summary.distance_since_codes_cleared_km} km"))
    if summary.warmups_since_codes_cleared is not None:
        rows.append(("Warm-ups since codes cleared", str(summary.warmups_since_codes_cleared)))
    if summary.cold_start is not None:
        rows.append(("Cold start", f"{_yes_no(summary.cold_start)} (coolant {summary.coolant_c:g} \u00b0C)"))
    if summary.vin:
        rows.append(("VIN", summary.vin))
    rows.append(("Flags", _flag_text(summary.flags)))
    return rows, []


def _audio_details(summary: EngineAudioSummary) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if summary.duration_s is not None:
        rows.append(("Clip length", f"{summary.duration_s:g} s"))
    if summary.state == "assessed":
        rows.append(("Loudness", f"average {summary.rms_db:g} dBFS, peak {summary.peak_db:g} dBFS"))
        rows.append(("Loudness envelope", f"{len(summary.envelope_db)} readings, one per {summary.envelope_step_ms} ms "
                     f"(range {min(summary.envelope_db):g} to {max(summary.envelope_db):g} dBFS)"
                     if summary.envelope_db else "not available"))
    return rows


def _paid_check_rows(checks: Sequence[PaidSourceCheck],
                     media_url: Callable[[str], str | None] | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for check in checks:
        record: SourceRecord | None = check.source
        row: dict[str, object] = {"label": _SOURCE_LABELS[check.kind], "badge": None, "device": None,
                                  "status": "Not checked",
                                  "reason": "No data was attached for this check; it is not a pass.",
                                  "details": [], "windows": [], "audio_src": None, "spectrogram_src": None,
                                  "note": None}
        if record is not None:
            summary = record.summary
            row["badge"] = _PROVENANCE_BADGES[record.provenance]
            row["device"] = record.device
            if summary.state == "assessed":
                row["status"], row["reason"] = "Checked", None
            else:
                row["reason"] = f"{summary.reason or 'Data was insufficient.'} This is not a pass."
            if isinstance(summary, EngineAudioSummary):
                row["details"], row["windows"] = _audio_details(summary), []
                if summary.state == "assessed" and media_url is not None:
                    row["audio_src"] = _safe_path(media_url("audio"))
                    row["spectrogram_src"] = _safe_path(media_url("spectrogram")) if summary.spectrogram_sha256 else None
                if record.provenance == "synthetic":
                    row["note"] = ("This clip is a bench or internet sample (synthetic), not a recording of the "
                                   "vehicle in this report.")
            else:
                row["details"], row["windows"] = (_rig_details(summary) if isinstance(summary, RigTelemetrySummary)
                                                  else _obd_details(summary))
        rows.append(row)
    return rows


def _common_context(report: FreeReport | PaidReport, provenance_label: str | None) -> dict[str, object]:
    return {
        "css": Markup(_CSS),
        "vehicle_label": report.vehicle_label,
        "provenance_label": provenance_label,
        "state_notice": _state_notice(report.state),
        "revision_short": str(report.revision_id)[:8],
        "footer_extra": None,
        "view_width": VIEW_WIDTH,
        "view_height": VIEW_HEIGHT,
    }


def _free_context(report: FreeReport, evidence_url: EvidenceUrl | None, unlock_href: str | None) -> dict[str, object]:
    cards, numbers = _cards(report.findings, evidence_url)
    coverage = {row.zone: row for row in report.coverage}
    driver = coverage.get(Zone.DRIVER_SIDE)
    driver_assessed = driver is not None and driver.state == CoverageState.ASSESSED
    part1_reason = (_humanize(driver.reason) if driver is not None and driver.reason
                    else "The driver side was not fully assessed.")
    locked_rows = []
    for locked in sorted(report.locked_zones, key=lambda row: PART_ZONES.index(row.zone)):
        row = coverage.get(locked.zone)
        if row is None:
            summary = "Assessment status not available in the free view."
        elif row.state == CoverageState.ASSESSED and locked.finding_count == 0:
            summary = f"{NO_FINDINGS_PHRASE}. Details are in the full report."
        elif row.state == CoverageState.ASSESSED:
            summary = f"{locked.finding_count} finding{'s' if locked.finding_count != 1 else ''} recorded."
        else:
            summary = (f"{_STATE_LABELS[row.state]}; {locked.finding_count} finding"
                       f"{'s' if locked.finding_count != 1 else ''} recorded so far. This is not a clean result.")
        locked_rows.append({"zone_label": _zone_label(locked.zone), "summary": summary})
    if cards:
        no_findings = ""
    elif driver_assessed:
        no_findings = f"{NO_FINDINGS_PHRASE}."
    else:
        no_findings = "No findings are shown, and the driver side was not fully assessed, so this is not a clean result."
    context = _common_context(report, None)
    context.update({
        "part1_score": None if report.part1_score is None else _score_text(report.part1_score),
        "part1_reason": part1_reason,
        "part1_summary": report.part1_summary,
        "coverage_rows": _coverage_rows(report),
        "diagram_views": _diagram_views(report.findings, numbers),
        "cards": cards,
        "no_findings_text": no_findings,
        "locked_rows": locked_rows,
        "unlock_href": unlock_href,
        "check_rows": _free_check_rows(report.additional_checks),
    })
    return context


def _refs(ids: Sequence[UUID], numbers: dict[UUID, int]) -> list[int]:
    return sorted({numbers[item] for item in ids})


def _zone_rows(report: PaidReport, numbers: dict[UUID, int]) -> list[dict[str, object]]:
    coverage = {row.zone: row for row in report.coverage}
    scored = {row.zone: row for row in report.scores.zones}
    by_zone: dict[Zone, list[int]] = defaultdict(list)
    for finding in report.findings:
        by_zone[finding.zone].append(numbers[finding.id])
    rows = []
    for zone in PART_ZONES:
        row = scored.get(zone)
        refs = sorted(by_zone.get(zone, []))
        if row is None:
            rows.append({"zone_label": _zone_label(zone), "state": CoverageState.NOT_ASSESSED.value,
                         "state_label": _STATE_LABELS[CoverageState.NOT_ASSESSED],
                         "score_text": "Not scored", "note": "No assessment recorded.", "finding_refs": refs})
            continue
        reason = row.reason or (coverage[zone].reason if zone in coverage else None)
        if row.state == CoverageState.ASSESSED:
            note = "" if refs else f"{NO_FINDINGS_PHRASE}."
        else:
            note = (_humanize(reason) if reason else "Coverage incomplete") + ". Score withheld."
        rows.append({
            "zone_label": _zone_label(zone),
            "state": row.state.value,
            "state_label": _STATE_LABELS[row.state],
            "score_text": "Not scored" if row.score is None else _score_text(row.score),
            "note": note,
            "finding_refs": refs,
        })
    return rows


def _repairs_context(report: PaidReport, numbers: dict[UUID, int]) -> dict[str, object]:
    summary = report.repairs
    unknown = sum(item.low_cents is None for item in summary.items)
    unestimated = max(unknown, summary.unestimated_count)
    estimated = len(summary.items) - unknown
    rows = []
    for item in summary.items:
        rows.append({
            "component": ("Unidentified component" if item.component_id is None
                          else _COMPONENT_LABELS.get(item.component_id, _words(item.component_id.value).capitalize())),
            "operation": "No repair mapped" if item.operation is None else _words(item.operation.value).capitalize(),
            "severity": item.severity,
            "estimate": ("No estimate" if item.low_cents is None or item.high_cents is None
                         else f"{_money(item.low_cents)}–{_money(item.high_cents)}"),
            "reason": _humanize(item.reason) if item.reason and item.low_cents is None else "",
            "refs": _refs(item.finding_ids, numbers),
        })
    if estimated:
        total = f"{_money(summary.subtotal_low_cents)}–{_money(summary.subtotal_high_cents)}"
    else:
        total = "No item could be estimated"
    plural = "s have" if unestimated != 1 else " has"
    return {
        "title": "Subtotal of estimated repairs" if unestimated else "Estimated repairs",
        "rows": rows,
        "total_text": total,
        "unestimated_text": (f"{unestimated} item{plural} no estimate and {'is' if unestimated == 1 else 'are'} "
                             "not included in this subtotal." if unestimated else ""),
        "label": summary.label,
        "table_version": summary.table_version,
    }


def _paid_context(report: PaidReport, evidence_url: EvidenceUrl | None,
                  media_url: Callable[[str], str | None] | None = None) -> dict[str, object]:
    cards, numbers = _cards(report.findings, evidence_url)
    assessed = sum(row.score is not None for row in report.scores.zones)
    context = _common_context(report, None)
    footer = f"Scoring config {report.scores.config_hash[:12]}"
    if report.rendered_at is not None:
        footer += f" · Rendered {report.rendered_at.isoformat()}"
    context.update({
        "footer_extra": footer,
        "headline": None if report.scores.headline is None else _score_text(report.scores.headline),
        "assessed_count": assessed,
        "score_label": report.scores.label,
        "risk_cards": [card for card in cards if card.high_risk],
        "zone_rows": _zone_rows(report, numbers),
        "diagram_views": _diagram_views(report.findings, numbers),
        "cards": cards,
        "candidate_cards": [{"number": f"U{index}", "concept_label": _words(row.concept.value).capitalize(),
            "zone_label": _zone_label(row.zone), "evidence_note": row.evidence_note,
            "confidence_text": ("Confidence not available" if row.confidence is None else f"Model confidence {row.confidence:.2f}") + f" ({row.confidence_label})",
            "image_src": _safe_path(evidence_url(row.id)) if evidence_url is not None and row.evidence_available else None,
            "source_time": source_time_label(row.source_timestamp_ms)} for index, row in enumerate(sorted(report.candidates, key=lambda item: (PART_ZONES.index(item.zone), str(item.id))), start=1)],
        "no_findings_text": "No findings are included in this report.",
        "claim_rows": [{
            # Displayed 1-based, matching narration's "Listing claim N".
            "index": claim.claim_index + 1,
            "text": claim.text,
            "verdict": claim.verdict.value,
            "verdict_label": _VERDICT_LABELS[claim.verdict.value],
            "rationale": claim.rationale,
            "refs": _refs(claim.finding_ids, numbers),
        } for claim in sorted(report.claims, key=lambda row: row.claim_index)],
        "repairs": _repairs_context(report, numbers),
        "check_rows": _paid_check_rows(report.additional_checks, media_url),
        "narration_rows": [{"text": row.text, "refs": _refs(row.finding_ids, numbers)} for row in report.narration],
    })
    if report.scores.headline is None and assessed >= 3:
        context["score_label"] = f"{report.scores.label}; no overall index was computed"
    return context


def render_report(
    report: FreeReport | PaidReport,
    *,
    evidence_url: EvidenceUrl | None = None,
    unlock_href: str | None = None,
    provenance_label: str | None = None,
    source_media_url: Callable[[str], str | None] | None = None,
) -> str:
    """Render one scope-filtered report DTO to HTML.

    evidence_url maps a finding ID to the same-origin route of its approved public
    derivative, or None. A frame is only embedded when the finding is marked
    `evidence_available` and the route is a plain same-origin path; otherwise the card
    says "Evidence image withheld". The renderer never builds media URLs itself.
    unlock_href (free view only) must be a same-origin path.
    provenance_label is an optional banner such as "Synthetic fixture data".
    """
    if isinstance(report, FreeReport):
        if unlock_href is not None and _safe_path(unlock_href) is None:
            raise ValueError("unlock_href must be a same-origin path")
        context = _free_context(report, evidence_url, unlock_href)
        template = "free.html"
    elif isinstance(report, PaidReport):
        context = _paid_context(report, evidence_url, source_media_url)
        template = "paid.html"
    else:
        raise TypeError("render_report requires a FreeReport or PaidReport")
    context["provenance_label"] = provenance_label
    return _ENV.get_template(template).render(**context)


def render_report_status(report: ReportStatus) -> str:
    """Limited terminal/building page; no snapshot, score or evidence is read."""
    return _ENV.get_template("status.html").render(
        css=Markup(_CSS), vehicle_label=None, provenance_label=None, state_notice=_state_notice(report.state),
        revision_short=str(report.revision_id)[:8], footer_extra=None,
    )
