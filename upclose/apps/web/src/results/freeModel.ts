import type { FreeFinding, PublicCoverage } from '../api/client';
import type { PaidReport } from '../report/model';

/** Flags the backend uses for its own de-duplication; they never make a finding serious (mirrors isSerious in report/model.ts). */
const INTERNAL_FLAGS: ReadonlySet<string> = new Set(['duplicate_review', 'dedup_merged']);

/** Same rule as `isSerious` in report/model.ts, for free findings. */
export function isSeriousFree(finding: Pick<FreeFinding, 'flags'>): boolean {
  return finding.flags.some((flag) => !INTERNAL_FLAGS.has(flag));
}

export interface FreeVerdictLine {
  text: string;
  /** True only when something serious was found. Unknown or partial coverage is never "ok". */
  serious: boolean;
}

/** A factual one-liner from the free findings. It never claims the car is clean. */
export function freeVerdictLine(findings: readonly FreeFinding[], coverage: PublicCoverage | undefined): FreeVerdictLine {
  const serious = findings.filter(isSeriousFree).length;
  const minor = findings.length - serious;
  if (serious > 0) {
    return { serious: true, text: `${serious} serious ${serious === 1 ? 'issue' : 'issues'}${minor ? `, ${minor} minor` : ''}` };
  }
  if (minor > 0) return { serious: false, text: `${minor} minor ${minor === 1 ? 'finding' : 'findings'}, none flagged as serious` };
  if (coverage?.state === 'assessed') return { serious: false, text: 'Nothing found on the driver side' };
  return { serious: false, text: 'Driver side not fully assessed' };
}

const SYNTHETIC = /synthetic/i;

/** Free results carry no provenance field; the server's own text says "synthetic" for fixture results. */
export function isSyntheticFree(data: { part1_summary: string; findings: readonly FreeFinding[] }): boolean {
  return SYNTHETIC.test(data.part1_summary) || data.findings.some((finding) => SYNTHETIC.test(finding.evidence_note));
}

export function isSyntheticPaid(report: PaidReport): boolean {
  return report.findings.some((finding) => SYNTHETIC.test(finding.evidence_note))
    || report.narration.some((line) => SYNTHETIC.test(line.text))
    || report.claims.some((claim) => SYNTHETIC.test(claim.rationale));
}
