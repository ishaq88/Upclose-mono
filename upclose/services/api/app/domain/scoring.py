from decimal import Decimal, localcontext

from app.contracts.enums import PART_ZONES, CoverageState, FindingStatus
from app.contracts.scoring import ScoreResult, ScoringInput, ZoneScore

from .config import ScoringConfig, ZoneConfig


def _zone_coverage(inputs: ScoringInput, config: ZoneConfig) -> tuple[CoverageState, str | None]:
    if not config.required_concepts:
        return CoverageState.NOT_ASSESSED, "No enabled assessment concepts for this zone."
    coverage = [row for row in inputs.coverage if row.zone == config.zone and row.concept in config.required_concepts]
    findings = [row for row in inputs.findings if row.zone == config.zone]
    if not coverage and not findings:
        return CoverageState.NOT_ASSESSED, "No assessment evidence supplied."
    if any(row.status in (FindingStatus.CANDIDATE, FindingStatus.UNVERIFIED) for row in findings):
        return CoverageState.PARTIAL, "Unresolved candidate or unverified finding."
    concepts = {row.concept for row in coverage}
    if concepts != set(config.required_concepts) or any(row.state != CoverageState.ASSESSED for row in coverage):
        return CoverageState.PARTIAL, "Required concept coverage is missing or incomplete."
    # Each supplied finding must be backed by assessed coverage in its own run.
    assessed = {(row.run_id, row.concept) for row in coverage if row.state == CoverageState.ASSESSED}
    if any((row.run_id, row.concept) not in assessed for row in findings):
        return CoverageState.PARTIAL, "Finding lacks assessed coverage for its run and concept."
    return CoverageState.ASSESSED, None


def score_assessment(inputs: ScoringInput, config: ScoringConfig) -> ScoreResult:
    """Score frozen inputs; callers retain the exact input/config snapshot."""
    weights = {row.concept: row.weight for row in config.concepts}
    by_zone = {row.zone: row for row in config.zones}
    zones: list[ZoneScore] = []
    with localcontext() as context:
        context.prec = 50
        for zone in PART_ZONES:
            state, reason = _zone_coverage(inputs, by_zone[zone])
            score = None
            if state == CoverageState.ASSESSED:
                verified = sorted((row for row in inputs.findings if row.zone == zone
                                   and row.status == FindingStatus.VERIFIED and row.duplicate_of is None), key=lambda row: str(row.id))
                deduction = sum((Decimal(row.severity) * weights[row.concept] for row in verified), Decimal(0))
                score = Decimal(100) - min(deduction, config.deduction_cap)
                if not verified:
                    reason = "No visible findings detected in assessed footage."
            zones.append(ZoneScore(zone=zone, state=state, score=score, reason=reason))
        assessed = [row for row in zones if row.score is not None]
        headline = None
        fill = config.unassessed_zone_score
        if fill is not None:
            # Missing-input mode: every scorable zone counts, an unseen one at the configured value.
            scorable = [row for row in zones if by_zone[row.zone].required_concepts]
            denominator = sum((by_zone[row.zone].weight for row in scorable), Decimal(0))
            headline = sum(((row.score if row.score is not None else fill) * by_zone[row.zone].weight
                            for row in scorable), Decimal(0)) / denominator
            return ScoreResult(headline=headline, zones=tuple(zones), config_hash=config.content_hash,
                               unassessed_zone_score=fill)
        if len(assessed) >= config.minimum_headline_zones:
            denominator = sum((by_zone[row.zone].weight for row in assessed), Decimal(0))
            headline = sum((row.score * by_zone[row.zone].weight for row in assessed), Decimal(0)) / denominator
        return ScoreResult(headline=headline, zones=tuple(zones), config_hash=config.content_hash)


def visual_reason(result: ScoreResult, config: ScoringConfig) -> str | None:
    """Says how many scorable zones were not seen and counted at the fill value; None in the original mode."""
    fill = config.unassessed_zone_score
    if fill is None:
        return None
    scorable = [row for row in result.zones if next(z for z in config.zones if z.zone == row.zone).required_concepts]
    unseen = sum(row.score is None for row in scorable)
    if unseen == 0:
        return None
    return f"{unseen} of {len(scorable)} zones not seen; each counted as {fill.normalize():f}."
