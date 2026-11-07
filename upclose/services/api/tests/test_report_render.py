"""Pure renderer checks over synthetic DTOs; no database, network or JavaScript."""

import base64
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from app.contracts.enums import (
    PART_ZONES, ClaimVerdict, ComponentId, Concept, CoverageState, RepairOperation, ReportState, Subzone, Zone,
)
from app.contracts.evidence import BoundingBox, NarratedStatement
from app.contracts.reports import (
    FreeFinding, FreeReport, LockedZoneSummary, PaidFinding, PaidReport, PublicClaim, PublicCoverage,
)
from app.contracts.scoring import RepairEstimate, RepairSummary, ScoreResult, ZoneScore
from app.report import PIN_ANCHOR_VERSION, REPORT_CSP, REPORT_HEADERS, build_environment, render_report
from app.report.anchors import (
    COMPONENT_ANCHORS, SUBZONE_ANCHORS, VIEW_HEIGHT, VIEW_WIDTH, VIEWS, ZONE_ANCHORS, anchor_for,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAPTION = "Approximate diagram — pin positions are not measured locations."
NO_FINDINGS = "No visible findings detected in assessed footage"
BBOX = BoundingBox(x=0.1, y=0.1, w=0.2, h=0.2)
REVISION = UUID("00000000-0000-4000-8000-000000000007")


def fid(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


def finding(n: int, zone: Zone, concept: Concept, severity: int = 3, *, component=None, subzone=None,
            flags=(), note="Synthetic note.", evidence=False, cls=PaidFinding) -> PaidFinding:
    return cls(id=fid(n), zone=zone, concept=concept, severity=severity, confidence=0.7, subzone=subzone,
               component_id=component, flags=tuple(flags), bbox=BBOX, evidence_available=evidence, evidence_note=note)


def coverage(*pairs) -> tuple[PublicCoverage, ...]:
    return tuple(PublicCoverage(zone=zone, state=state, reason=reason) for zone, state, reason in pairs)


def paid_findings() -> tuple[PaidFinding, ...]:
    return (
        # Intentionally unordered: cards must sort by part number then ID.
        finding(31, Zone.UNDERBODY, Concept.FLUID_LEAK, 4, note="Synthetic underbody drip UNDER-NOTE."),
        finding(12, Zone.PASSENGER_SIDE, Concept.DENT, 3, component=ComponentId.PASSENGER_FRONT_DOOR,
                subzone=Subzone.DOOR, note="Synthetic passenger door dent PASS-NOTE."),
        finding(11, Zone.DRIVER_SIDE, Concept.RUST, 5, component=ComponentId.DRIVER_ROCKER, subzone=Subzone.ROCKER,
                flags=("structural_rust_review", "dedup_merged"), note="Synthetic rocker rust DRV-NOTE.", evidence=True),
        finding(10, Zone.DRIVER_SIDE, Concept.SCRATCH, 1, note="Synthetic driver scratch."),
        finding(21, Zone.TIRES, Concept.TIRE_WEAR, 3, component=ComponentId.TIRE_FL, note="Synthetic tire wear TIRE-NOTE."),
    )


def paid_report(*, headline: Decimal | None = Decimal("91.2"), findings=None, zones=None) -> PaidReport:
    findings = paid_findings() if findings is None else findings
    if zones is None:
        zones = (
            ZoneScore(zone=Zone.DRIVER_SIDE, state=CoverageState.ASSESSED, score=Decimal("80.0")),
            ZoneScore(zone=Zone.PASSENGER_SIDE, state=CoverageState.ASSESSED, score=Decimal("93.0")),
            ZoneScore(zone=Zone.FRONT_REAR_BODY, state=CoverageState.ASSESSED, score=Decimal("100"),
                      reason="No visible findings detected in assessed footage."),
            ZoneScore(zone=Zone.TIRES, state=CoverageState.PARTIAL, score=None, reason="blurry_footage"),
            ZoneScore(zone=Zone.UNDERBODY, state=CoverageState.NOT_ASSESSED, score=None, reason="part_skipped"),
        )
    ids = {row.id for row in findings}
    rust = fid(11)
    return PaidReport(
        slug="synthetic-report", revision_id=REVISION, state=ReportState.READY,
        coverage=coverage((Zone.DRIVER_SIDE, CoverageState.ASSESSED, None),
                          (Zone.TIRES, CoverageState.PARTIAL, "blurry_footage"),
                          (Zone.UNDERBODY, CoverageState.NOT_ASSESSED, "part_skipped")),
        scores=ScoreResult(headline=headline, zones=zones, config_hash="a" * 64),
        findings=tuple(findings),
        repairs=RepairSummary(
            items=(
                RepairEstimate(operation=RepairOperation.REPLACE_TIRE, component_id=ComponentId.TIRE_FL, severity=3,
                               finding_ids=(fid(21),) if fid(21) in ids else (), low_cents=9000, high_cents=16000, reason=None),
                RepairEstimate(operation=None, component_id=ComponentId.DRIVER_ROCKER, severity=5,
                               finding_ids=(rust,) if rust in ids else (), low_cents=None, high_cents=None,
                               reason="structural_rust_not_estimated"),
            ),
            subtotal_low_cents=9000, subtotal_high_cents=16000, unestimated_count=1, table_version="synthetic-planning-v1"),
        claims=(
            PublicClaim(claim_index=1, text="Seller says: no rust anywhere CLAIM-TEXT-ONE", verdict=ClaimVerdict.CONTRADICTED,
                        finding_ids=(rust,) if rust in ids else (), rationale="Visible rust on rocker CLAIM-WHY."),
            PublicClaim(claim_index=0, text="Garage kept CLAIM-TEXT-ZERO", verdict=ClaimVerdict.NOT_VISIBLE,
                        finding_ids=(), rationale="Not observable."),
        ) if rust in ids else (),
        narration=(NarratedStatement(text="Narration sentence NARR-ONE about rust.", finding_ids=(rust,)),) if rust in ids else (),
    )


def free_from(paid: PaidReport) -> FreeReport:
    driver = tuple(FreeFinding.model_validate(row.model_dump()) for row in paid.findings if row.zone == Zone.DRIVER_SIDE)
    others = [zone for zone in PART_ZONES if zone != Zone.DRIVER_SIDE]
    counts = {zone: sum(row.zone == zone for row in paid.findings) for zone in others}
    return FreeReport(
        slug="synthetic-report", revision_id=REVISION, state=ReportState.READY, coverage=paid.coverage,
        part1_score=Decimal("80.0"), findings=driver,
        locked_zones=tuple(LockedZoneSummary(zone=zone, finding_count=counts[zone]) for zone in others),
        part1_summary="Part-1 summary text P1-SUM.")


def evidence_route(finding_id: UUID) -> str:
    return f"/r/synthetic-report/frames/{finding_id}"


# ---- no-JS, escaping, meta, determinism -------------------------------------------------------

@pytest.mark.parametrize("make", [lambda: free_from(paid_report()), paid_report,
                                   lambda: FreeReport.model_validate(fixture("free_report")),
                                   lambda: PaidReport.model_validate(fixture("paid_report"))])
def test_no_javascript_or_style_attributes_and_required_meta(make) -> None:
    html = render_report(make(), evidence_url=evidence_route, unlock_href="/unlock")
    lowered = html.lower()
    assert "<script" not in lowered and "javascript:" not in lowered
    assert not re.search(r"\son[a-z]+\s*=", lowered)
    assert not re.search(r"<[^>]+\sstyle\s*=", lowered)
    assert not re.search(r"(src|href)=\"(https?:)?//", lowered) and "@import" not in lowered and "url(" not in lowered
    assert '<meta name="robots" content="noindex,nofollow">' in html
    assert '<meta name="referrer" content="no-referrer">' in html
    assert "@media print" in html and "@page" in html
    assert html.startswith("<!doctype html>") and '<html lang="en">' in html


def test_output_is_deterministic_and_input_order_independent() -> None:
    paid = paid_report()
    assert render_report(paid) == render_report(paid)
    reordered = paid_report(findings=tuple(reversed(paid.findings)))
    assert render_report(reordered) == render_report(paid)
    free = free_from(paid)
    assert render_report(free) == render_report(free)


def test_untrusted_text_is_escaped_everywhere() -> None:
    evil = '<script>alert(1)</script> "q" \'s\' <img src=x onerror=y>'
    paid = paid_report()
    findings = (finding(11, Zone.DRIVER_SIDE, Concept.RUST, 5, flags=('<b>"x"',), note=evil),)
    claim = PublicClaim(claim_index=1, text=evil, verdict=ClaimVerdict.SUPPORTED, finding_ids=(fid(11),), rationale=evil)
    narration = NarratedStatement(text=evil, finding_ids=(fid(11),))
    evil_paid = paid.model_copy(update={
        "findings": findings, "claims": (claim,), "narration": (narration,),
        "repairs": RepairSummary(items=(), subtotal_low_cents=0, subtotal_high_cents=0, unestimated_count=0,
                                 table_version='v"<1>', label="Local planning estimate; not a current shop quote"),
    })
    html = render_report(evil_paid)
    assert "<script" not in html.lower() and "<img src=x" not in html and "<b>" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 4
    free = FreeReport.model_validate(fixture("free_report")).model_copy(update={"part1_summary": evil})
    free_html = render_report(free)
    assert "<script" not in free_html.lower() and "&lt;script&gt;" in free_html


def test_environment_is_strict_autoescaped_and_package_scoped() -> None:
    from jinja2 import StrictUndefined
    env = build_environment()
    assert env.autoescape is True and env.undefined is StrictUndefined
    assert env.loader.searchpath and all(path.replace("\\", "/").endswith("app/templates/report") for path in env.loader.searchpath)
    from jinja2 import TemplateNotFound
    with pytest.raises(TemplateNotFound):
        env.get_template("../../report/render.py")


def test_csp_hash_matches_inline_style_and_response_headers() -> None:
    html = render_report(paid_report())
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in REPORT_CSP
    assert html.count("<style>") == 1
    for needle in ("default-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'", "img-src 'self'"):
        assert needle in REPORT_CSP
    assert "script-src" not in REPORT_CSP and "unsafe-inline" not in REPORT_CSP
    assert REPORT_HEADERS["Cache-Control"] == "private, no-store"
    assert REPORT_HEADERS["Content-Security-Policy"] == REPORT_CSP
    assert "noindex" in REPORT_HEADERS["X-Robots-Tag"] and REPORT_HEADERS["Referrer-Policy"] == "no-referrer"


# ---- free scope leakage ------------------------------------------------------------------------

def test_free_html_contains_nothing_from_locked_paid_content() -> None:
    paid = paid_report()
    free = free_from(paid)
    html = render_report(free, evidence_url=evidence_route, unlock_href="/unlock/synthetic")
    forbidden = [str(row.id) for row in paid.findings if row.zone != Zone.DRIVER_SIDE]
    forbidden += [row.evidence_note for row in paid.findings if row.zone != Zone.DRIVER_SIDE]
    forbidden += [claim.text for claim in paid.claims] + [claim.rationale for claim in paid.claims]
    forbidden += [row.text for row in paid.narration]
    forbidden += [str(paid.scores.headline), paid.scores.config_hash, paid.repairs.table_version, "synthetic-planning-v1"]
    forbidden += ["$90", "$160", "$", "Subtotal", "structural_rust_not_estimated", "Estimated repairs", "93.0", "CLAIM-", "NARR-"]
    for value in forbidden:
        assert value not in html, value
    assert str(fid(11)) in html  # only inside the caller-supplied frame route
    assert str(fid(11)) not in render_report(free)  # IDs are never printed in text; cards use numbers
    assert "Locked" in html and "/unlock/synthetic" in html


def test_free_locked_zones_are_totals_only_and_never_read_as_clean() -> None:
    html = render_report(free_from(paid_report()))
    assert "Passenger side" in html and "1 finding recorded." not in html.split("Locked zones")[0]
    locked = html.split('id="locked-h"')[1]
    assert "Tires" in locked and "This is not a clean result" in locked
    assert "dent" not in locked.lower() and "severity" not in locked.lower()
    assert locked.count('class="locked"') == 7


def test_free_fixture_renders_part1_only() -> None:
    free = FreeReport.model_validate(fixture("free_report"))
    html = render_report(free)
    assert "85.0" in html and "Provisional visual-condition index" in html
    assert "Evidence image withheld" in html
    assert "Uncalibrated model confidence, not an accuracy probability" in html
    assert "Passenger side" in html and "Locked" in html
    assert html.count('class="car-view"') == 5


def test_free_null_part1_score_is_not_scored_with_reason() -> None:
    free = FreeReport.model_validate(fixture("free_report")).model_copy(update={"part1_score": None, "findings": ()})
    free = free.model_copy(update={"coverage": coverage((Zone.DRIVER_SIDE, CoverageState.PARTIAL, "blurry_footage"))})
    html = render_report(free)
    assert "Not scored" in html and "Blurry footage" in html
    assert re.search(r"score-value\">(0|100)(\.0)?<", html) is None
    assert NO_FINDINGS not in html and "not a clean result" in html


def test_free_assessed_zero_findings_uses_exact_phrase() -> None:
    free = FreeReport.model_validate(fixture("free_report")).model_copy(update={"part1_score": Decimal("100"), "findings": ()})
    html = render_report(free)
    assert f"{NO_FINDINGS}." in html and "damage free." not in html.replace("Absence of a finding does not mean damage free.", "")


def test_free_unlock_href_must_be_same_origin_path() -> None:
    free = FreeReport.model_validate(fixture("free_report"))
    for bad in ("https://evil.example/x", "//evil.example", "javascript:alert(1)", '/x" onclick="y'):
        with pytest.raises(ValueError):
            render_report(free, unlock_href=bad)


# ---- paid view ---------------------------------------------------------------------------------

def test_paid_report_shows_full_content() -> None:
    paid = paid_report()
    html = render_report(paid, evidence_url=evidence_route, provenance_label="Synthetic fixture data")
    for needle in ("91.2", "CLAIM-TEXT-ONE", "CLAIM-WHY", "NARR-ONE", "UNDER-NOTE", "PASS-NOTE", "synthetic-planning-v1",
                   "Local planning estimate; not a current shop quote", "Subtotal of estimated repairs", "$90–$160",
                   "No estimate", "Structural rust not estimated", "Contradicted by the assessed footage",
                   "Not visible in the assessed footage", "Provisional visual-condition index",
                   "Uncalibrated model confidence, not an accuracy probability", "Synthetic fixture data"):
        assert needle in html, needle
    assert "1 item has no estimate and is not included in this subtotal." in html
    # Claims sort by index; unknown flags display, internal bookkeeping flags do not.
    assert html.index("CLAIM-TEXT-ZERO") < html.index("CLAIM-TEXT-ONE")
    # Claim rows are 1-based so they match narration's "Listing claim N".
    assert '<td class="num">1</td><td>Garage kept CLAIM-TEXT-ZERO' in html
    assert '<td class="num">0</td>' not in html
    assert "Structural rust review" in html and "Dedup merged" not in html
    assert "Scoring config " + "a" * 12 in html


def test_paid_high_risk_flags_lead_the_report() -> None:
    html = render_report(paid_report())
    assert html.index("High-risk flags") < html.index("Zone scores and coverage") < html.index('id="findings-h"')
    # Flags stay ahead even with a high headline.
    assert html.index("High-risk flags") < html.index("Overall") + 2000


def test_paid_without_flags_has_no_risk_section() -> None:
    findings = (finding(10, Zone.DRIVER_SIDE, Concept.SCRATCH, 1, flags=("duplicate_review",)),)
    html = render_report(paid_report(findings=findings))
    assert "High-risk flags" not in html


def test_null_headline_and_null_zone_scores_never_render_zero_or_hundred() -> None:
    zones = (
        ZoneScore(zone=Zone.DRIVER_SIDE, state=CoverageState.ASSESSED, score=Decimal("80.0")),
        ZoneScore(zone=Zone.TIRES, state=CoverageState.PARTIAL, score=None, reason="blurry_footage"),
        ZoneScore(zone=Zone.UNDERBODY, state=CoverageState.NOT_ASSESSED, score=None, reason="part_skipped"),
    )
    html = render_report(paid_report(headline=None, zones=zones))
    assert "No overall index — fewer than 3 zones assessed" in html
    rows = re.findall(r"<tr><th scope=\"row\">([^<]+)</th><td class=\"state-(\w+)\">[^<]+</td><td class=\"num\">([^<]+)</td>", html)
    by_zone = {zone: (state, score) for zone, state, score in rows}
    assert by_zone["Tires"] == ("partial", "Not scored")
    assert by_zone["Underbody"] == ("not_assessed", "Not scored")
    assert by_zone["Passenger side"] == ("not_assessed", "Not scored")  # missing zone is never a clean 100
    assert by_zone["Driver side"][1] == "80.0"
    assert "Blurry footage. Score withheld." in html and "Part skipped. Score withheld." in html


def test_assessed_zone_without_findings_uses_exact_phrase() -> None:
    html = render_report(paid_report())
    row = re.search(r"<th scope=\"row\">Front and rear body</th>.*?</tr>", html).group(0)
    assert NO_FINDINGS + "." in row and ">100.0<" in row
    assert "damage free" not in html.replace("does not mean damage free", "")


def test_repairs_wording_with_and_without_unknowns() -> None:
    html = render_report(paid_report())
    assert "<h2 id=\"repairs-h\">Subtotal of estimated repairs</h2>" in html
    all_known = paid_report().model_copy(update={"repairs": RepairSummary(
        items=(RepairEstimate(operation=RepairOperation.REPLACE_TIRE, component_id=ComponentId.TIRE_FL, severity=3,
                              finding_ids=(fid(21),), low_cents=9000, high_cents=16050, reason=None),),
        subtotal_low_cents=9000, subtotal_high_cents=16050, unestimated_count=0, table_version="t1")})
    html = render_report(all_known)
    assert "<h2 id=\"repairs-h\">Estimated repairs</h2>" in html and "$90–$160.50" in html and "not included" not in html
    none_known = paid_report().model_copy(update={"repairs": RepairSummary(
        items=(RepairEstimate(operation=None, component_id=None, severity=2, finding_ids=(), low_cents=None,
                              high_cents=None, reason="uncertain_panel"),),
        subtotal_low_cents=0, subtotal_high_cents=0, unestimated_count=1, table_version="t1")})
    html = render_report(none_known)
    assert "No item could be estimated" in html and "$0" not in html and "Unidentified component" in html


def test_paid_fixture_renders_with_unknown_repair() -> None:
    html = render_report(PaidReport.model_validate(fixture("paid_report")))
    assert "No overall index — fewer than 3 zones assessed" in html
    assert "Subtotal of estimated repairs" in html and "synthetic-planning-v1" in html and "$0" not in html
    assert "Evidence image withheld" in html and "No listing claims were checked." in html


# ---- diagram, pins, cards ----------------------------------------------------------------------

def pins_in(html: str) -> list[tuple[str, int]]:
    found = []
    for match in re.finditer(r"<svg class=\"car-view\" data-view=\"(\w+)\".*?</svg>", html, re.S):
        for pin in re.findall(r"<g class=\"pin sev-\d\"[^>]*><title>Finding (\d+):", match.group(0)):
            found.append((match.group(1), int(pin)))
    return found


def test_pin_numbers_equal_card_numbers_and_order_is_part_then_id() -> None:
    html = render_report(paid_report())
    cards = [(int(n), zone) for n, zone in re.findall(r"id=\"finding-(\d+)\">.*?</span>[^<]*— ([^<]+)</h3>", html, re.S)]
    assert [n for n, _ in cards] == [1, 2, 3, 4, 5]
    assert [zone for _, zone in cards] == ["Driver side", "Driver side", "Passenger side", "Tires", "Underbody"]
    pins = pins_in(html)
    assert sorted(number for _, number in pins) == [1, 2, 3, 4, 5]
    # Card 1 is the lowest ID in the driver zone (scratch, id 10); card 2 is the flagged rocker rust (id 11).
    assert re.search(r"id=\"finding-1\">.*?Scratch", html, re.S) and re.search(r"id=\"finding-2\">.*?Rust", html, re.S)
    assert 'href="#finding-2">Finding 2</a>: Rust' in html  # flag section cites the same number


def test_pins_only_for_findings_present_and_free_is_part1_only() -> None:
    paid = paid_report()
    free_html = render_report(free_from(paid))
    pins = pins_in(free_html)
    assert sorted(number for _, number in pins) == [1, 2]
    assert {view for view, _ in pins} == {"left"}
    none_html = render_report(free_from(paid).model_copy(update={"findings": ()}))
    assert pins_in(none_html) == []


def test_diagram_is_one_generic_five_view_set_with_caption() -> None:
    for report in (free_from(paid_report()), paid_report()):
        html = render_report(report)
        assert html.count(CAPTION) == 1
        assert html.count('class="car-view"') == 5
        assert re.findall(r'data-view="(\w+)"', html) == ["top", "left", "right", "front", "rear"]
        assert html.count('id="defect-map"') == 1
        assert html.count("<svg") == 5  # nothing else is drawn as SVG; no body-style variants


def test_anchor_lookup_component_then_subzone_then_zone() -> None:
    assert PIN_ANCHOR_VERSION == "report-pin-anchors-v1"
    rocker = anchor_for(Zone.DRIVER_SIDE, Subzone.ROCKER, ComponentId.DRIVER_ROCKER)
    assert rocker == COMPONENT_ANCHORS[ComponentId.DRIVER_ROCKER] and rocker.view == "left"
    assert anchor_for(Zone.PASSENGER_SIDE, Subzone.DOOR, None).view == "right"
    unknown_component = anchor_for(Zone.DRIVER_SIDE, None, None)
    assert unknown_component == ZONE_ANCHORS[Zone.DRIVER_SIDE]
    assert anchor_for(Zone.UNDERBODY, Subzone.FENDER, None) == ZONE_ANCHORS[Zone.UNDERBODY]  # unmapped subzone -> zone
    for anchor in (*COMPONENT_ANCHORS.values(), *SUBZONE_ANCHORS.values(), *ZONE_ANCHORS.values()):
        assert anchor.view in VIEWS and 0 <= anchor.x <= VIEW_WIDTH and 0 <= anchor.y <= VIEW_HEIGHT
    for zone in Zone:
        assert zone in ZONE_ANCHORS
    for component in ComponentId:
        assert component in COMPONENT_ANCHORS


def test_pins_sharing_an_anchor_are_offset_not_stacked() -> None:
    findings = tuple(finding(n, Zone.UNDERBODY, Concept.RUST, 2) for n in (1, 2, 3))
    html = render_report(paid_report(findings=findings))
    positions = re.findall(r"<g class=\"pin sev-2\" transform=\"translate\(([\d.]+) ([\d.]+)\)\"", html)
    assert len(positions) == 3 and len(set(positions)) == 3


# ---- evidence images ---------------------------------------------------------------------------

def test_image_only_when_dto_marks_available_and_route_is_same_origin() -> None:
    paid = paid_report()
    html = render_report(paid, evidence_url=evidence_route)
    assert html.count("<img ") == 1 and f'src="/r/synthetic-report/frames/{fid(11)}"' in html
    assert html.count("Evidence image withheld") == 4  # every other card
    withheld = render_report(paid)  # no route supplied -> everything withheld
    assert "<img" not in withheld and withheld.count("Evidence image withheld") == 5
    for bad in ("https://cdn.example/x.jpg", "//cdn.example/x.jpg", "javascript:alert(1)", "data:image/png;base64,AA",
                "/x.jpg\" onerror=\"y", None, "s3://bucket/key", "frames/relative.jpg"):
        html = render_report(paid, evidence_url=lambda _id, bad=bad: bad)
        assert "<img" not in html and html.count("Evidence image withheld") == 5, bad
    never = render_report(paid, evidence_url=lambda _id: "/ok.jpg")
    assert never.count("<img ") == 1  # unavailable findings never get an image even if a route is offered


def test_evidence_callback_is_not_consulted_for_unavailable_findings() -> None:
    asked: list[UUID] = []
    render_report(paid_report(), evidence_url=lambda item: asked.append(item) or "/f")
    assert asked == [fid(11)]


# ---- state banners -----------------------------------------------------------------------------

def test_building_and_failed_states_are_announced() -> None:
    building = FreeReport.model_validate(fixture("free_report")).model_copy(update={"state": ReportState.BUILDING})
    assert "still being prepared" in render_report(building)
    failed = paid_report().model_copy(update={"state": ReportState.FAILED})
    assert "could not be completed" in render_report(failed)
    assert "still being prepared" not in render_report(paid_report())
