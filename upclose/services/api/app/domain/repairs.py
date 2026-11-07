from decimal import Decimal, ROUND_HALF_UP, localcontext

from app.contracts.enums import Concept, FindingStatus
from app.contracts.evidence import Finding
from app.contracts.scoring import RepairEstimate, RepairSummary, ScoringInput

from .config import CostConfig, CostRow


def _blocked(finding: Finding, config: CostConfig) -> bool:
    # Initial rubric treats severity-5 rust as visible perforation. Flags also
    # fence lower-severity structural/perforating damage from numeric quotes.
    return (finding.concept == Concept.RUST and finding.severity == 5) or any(
        fragment.lower() in flag.lower() for flag in finding.flags for fragment in config.blocked_flag_fragments
    )


def _cents(dollars: Decimal, multiplier: Decimal) -> int:
    return int((dollars * multiplier / Decimal(10)).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 1000)


def estimate_repairs(inputs: ScoringInput, config: CostConfig) -> RepairSummary:
    """Estimate supported verified repairs, never substitute zero for unknown."""
    verified = sorted((finding for finding in inputs.findings if finding.status == FindingStatus.VERIFIED
                       and finding.duplicate_of is None), key=lambda finding: str(finding.id))
    blocked_components = {finding.component_id for finding in verified if finding.component_id and _blocked(finding, config)}
    rows = {(component, concept): row for row in config.rows for component in row.components for concept in row.concepts}
    groups: dict[tuple[str, str], tuple[CostRow, list[Finding]]] = {}
    items: list[RepairEstimate] = []
    for finding in verified:
        row = rows.get((finding.component_id, finding.concept))
        reason = None
        if _blocked(finding, config) or finding.component_id in blocked_components:
            reason = "Structural or perforating damage requires an in-person repair assessment."
        elif finding.component_id is None:
            reason = "Component identity is unknown."
        elif row is None:
            reason = "No supported repair mapping for this concept and component."
        if reason:
            items.append(RepairEstimate(operation=None, component_id=finding.component_id, severity=finding.severity,
                                        finding_ids=(finding.id,), low_cents=None, high_cents=None, reason=reason))
            continue
        key = (row.operation.value, finding.component_id.value)
        if key not in groups:
            groups[key] = (row, [])
        groups[key][1].append(finding)
    multipliers = {row.severity: row.multiplier for row in config.severity_multipliers}
    with localcontext() as context:
        context.prec = 50
        for key in sorted(groups):
            row, findings = groups[key]
            severity = max(finding.severity for finding in findings)
            items.append(RepairEstimate(operation=row.operation, component_id=findings[0].component_id,
                                        severity=severity, finding_ids=tuple(finding.id for finding in findings),
                                        low_cents=_cents(row.low_dollars, multipliers[severity]),
                                        high_cents=_cents(row.high_dollars, multipliers[severity]), reason=None))
    items.sort(key=lambda item: (item.operation.value if item.operation else "~", item.component_id.value if item.component_id else "~", str(item.finding_ids[0])))
    unknown = sum(item.low_cents is None for item in items)
    title = "Subtotal of estimated repairs" if unknown else "Estimated repairs"
    return RepairSummary(items=tuple(items), subtotal_low_cents=sum(item.low_cents or 0 for item in items),
                         subtotal_high_cents=sum(item.high_cents or 0 for item in items), unestimated_count=unknown,
                         table_version=config.version, label=f"{title}; local planning estimate, not a current shop quote")
