import type { ReactElement } from 'react';
import type { CategoryScores as CategoryScoresData, FreeReport, FreeFinding, LockedZoneSummary, Part1Verdict, PartSummary, PublicCoverage } from '../api/client';
import type { UiError } from '../errors';
import { conceptLabel, COVERAGE_LABEL, zoneTitle } from '../progress/labels';
import { describeReason } from '../progress/reasons';
import { formatIndex } from '../report/model';
import { freeVerdictLine, isSeriousFree, isSyntheticFree } from '../results/freeModel';
import { ResultError } from '../results/ResultError';
import { Button, Card, Disclosure, Pill, ScoreTiles } from '../ui';
import '../results/results.css';

/** Exact wording required for an assessed zone with no verified findings. Never "damage free". */
export const NO_FINDINGS_TEXT = 'No visible findings detected in assessed footage';

/** The free (driver-side) data, as carried by both the progress verdict and the FreeReport. */
export type FreeData = Pick<Part1Verdict, 'part1_score' | 'part1_summary' | 'findings' | 'coverage' | 'locked_zones'> & { categories?: CategoryScoresData | null; dashboard?: FreeReport['dashboard'] };

export interface UnlockControl {
  onUnlock: () => void;
  busy: boolean;
  error: UiError | null;
}

export interface Part1SlotProps {
  /** Part 1 as the server reports it (undefined until the first poll lands). */
  part1: PartSummary | undefined;
  verdict: Part1Verdict | null | undefined;
}

/**
 * The Part-1 free-verdict slot, rendered between the tab notice and the capture steps.
 *
 * Capture is never interrupted: the slot exists from the first render as one stable section, so when the
 * verdict arrives the section is filled in place instead of an element being inserted above the record
 * button. It is a collapsed disclosure (the user opens it), not a modal; it never takes focus, and polling
 * updates re-render text only. Free restriction: this renders only the owner-progress free DTO (Part-1
 * findings, coverage, locked zone totals); no image is ever requested from here.
 */
export function Part1Slot({ part1, verdict }: Part1SlotProps): ReactElement {
  return (
    <Card as="section" padding="md" aria-labelledby="free-verdict-heading" data-testid="free-verdict-slot" className={`slot${verdict ? '' : ' slot-empty'}`}>
      {verdict ? <VerdictDisclosure verdict={verdict} /> : (
        <>
          <h2 id="free-verdict-heading" className="slot-title">Part 1 free result</h2>
          <p role="status" className="muted slot-status">{placeholderText(part1)}</p>
        </>
      )}
    </Card>
  );
}

function placeholderText(part1: PartSummary | undefined): string {
  if (!part1) return 'Waiting for the service to report Part 1.';
  switch (part1.state) {
    case 'pending': return 'Appears here once the Driver side clip is uploaded and processed. Keep filming meanwhile.';
    case 'uploading': return 'The Driver side clip is still uploading.';
    case 'uploaded':
    case 'processing': return 'The Driver side clip is being processed. This result appears here when it is ready.';
    case 'processed': return 'The Driver side clip is processed; the result has not been reported yet.';
    case 'skipped': return 'Not scored. The Driver side step was skipped, so there is no free result.';
    case 'failed': return `Not scored. ${describeReason(part1.reason)}`;
  }
}

function VerdictDisclosure({ verdict }: { verdict: Part1Verdict }): ReactElement {
  const findingCount = verdict.findings.length;
  return (
    <Disclosure
      className="slot-disc" title="Part 1 free result" titleId="free-verdict-heading" headingLevel={2} summaryTestId="verdict-headline" toggleLabel={['Show details', 'Hide details']}
      summary={`${verdict.part1_score === null ? 'Not scored' : `Provisional index ${formatIndex(verdict.part1_score)}`} · ${findingCount} ${findingCount === 1 ? 'finding' : 'findings'}`}>
      <p className="res-note">This card never moves your capture steps by itself.</p>
      <FreeVerdictCard data={verdict} layout="inline" />
    </Disclosure>
  );
}

export interface FreeVerdictCardProps {
  data: FreeData;
  /** `page`: two columns with a sticky unlock panel. `inline`: one column, for the capture screen. */
  layout: 'page' | 'inline';
  /** Without it no unlock button is shown (the capture screen has no report identity). */
  unlock?: UnlockControl;
}

/** Verdict line, provisional index, findings (text only), then the locked-zone totals and unlock. */
export function FreeVerdictCard({ data, layout, unlock }: FreeVerdictCardProps): ReactElement {
  const coverage = data.coverage[0];
  const line = freeVerdictLine(data.findings, coverage);
  const main = (
    <div className="res-col">
      {isSyntheticFree(data) && (
        <Pill tone="synthetic" data-testid="synthetic-badge">Synthetic test result. No real inference was performed.</Pill>
      )}
      <p className="vline" data-testid="verdict-line"><span className={`k${line.serious ? ' k-risk' : ''}`} aria-hidden="true" />{line.text}</p>
      <ScoreBlock score={data.part1_score} coverage={coverage} />
      {data.categories && <ScoreTiles categories={data.categories} />}
      {data.dashboard && <p data-testid="dashboard-summary">Dashboard: {data.dashboard.checked ? `Checked · ${data.dashboard.flag_count} flags` : 'Not checked. This is not a pass.'}</p>}
      <Findings findings={data.findings} coverage={coverage} />
      <p className="muted" data-testid="part1-summary">{data.part1_summary}</p>
      <p className="res-note">
        This is a provisional visual-condition screening of the Driver side only, not an inspection. It does not replace an in-person check by a qualified mechanic.
      </p>
    </div>
  );
  const aside = <UnlockPanel zones={data.locked_zones} unlock={unlock} />;
  if (layout === 'inline') {
    return <div className="free-inline">{main}{(unlock || data.locked_zones.length > 0) && aside}</div>;
  }
  return <div className="free-scope"><div className="free">{main}{aside}</div></div>;
}

function ScoreBlock({ score, coverage }: { score: number | null; coverage: PublicCoverage | undefined }): ReactElement {
  const reason = coverage?.reason ?? null;
  return (
    <div>
      <p className="muted fw">
        Provisional visual-condition index (Part 1, {zoneTitle('driver_side')})
      </p>
      {score === null ? (
        <div>
          <p className="score-none" data-testid="part1-score">Not scored</p>
          <p className="score-reason" data-testid="not-scored-reason">
            {reason ?? 'Coverage was not sufficient to give this zone a score. This is not a clean result.'}
          </p>
        </div>
      ) : (
        <p className="score-big" data-testid="part1-score">
          {formatIndex(score)} <span className="muted score-of">out of 100</span>
        </p>
      )}
      {coverage && (
        <p className="muted coverage-state" data-testid="coverage-state">
          Coverage: <b>{COVERAGE_LABEL[coverage.state]}</b>
          {score !== null && reason && !isNoFindingsReason(reason) ? `. ${reason}` : ''}
        </p>
      )}
    </div>
  );
}

function isNoFindingsReason(reason: string): boolean {
  return reason.replace(/\.$/, '').toLowerCase() === NO_FINDINGS_TEXT.toLowerCase();
}

function Findings({ findings, coverage }: { findings: readonly FreeFinding[]; coverage: PublicCoverage | undefined }): ReactElement {
  if (findings.length === 0) {
    return coverage?.state === 'assessed'
      ? <p className="fw-b" data-testid="no-findings">{NO_FINDINGS_TEXT}.</p>
      : <p data-testid="no-findings">No findings are listed, but this zone was not fully assessed, so this is not a clean result.</p>;
  }
  return (
    <ul className="fnd-list" aria-label="Part 1 findings">
      {findings.map((finding) => (
        <li key={finding.id} data-testid="free-finding"><Card padding="md" className="fnd">
          {isSeriousFree(finding) && <span className="risk-tag">Serious</span>}
          <h3>{conceptLabel(finding.concept)} <span className="muted fnd-sev">· severity {finding.severity}</span></h3>
          <p>{finding.evidence_note}</p>
          {finding.source_timestamp_ms != null && <p className="meta">Source video: {formatSourceTime(finding.source_timestamp_ms)}. {finding.mapping_basis === 'user_declared' && 'Section marked by uploader; area coverage unverified.'}</p>}
          <p className="meta">
            <span data-testid="confidence-label">{finding.confidence_label}</span>
            {finding.confidence === null ? null : <span>: {finding.confidence}</span>}
          </p>
          {finding.flags.length > 0 && <p className="meta">Flags: {finding.flags.join(', ')}</p>}
          <p className="hatch">{finding.evidence_available ? 'Evidence image not shown in this view' : 'Evidence image withheld'}</p>
        </Card></li>
      ))}
    </ul>
  );
}

function UnlockPanel({ zones, unlock }: { zones: readonly LockedZoneSummary[]; unlock: UnlockControl | undefined }): ReactElement {
  return (
    <Card as="aside" padding="lg" className="unlock" aria-labelledby="unlock-heading">
      <h2 id="unlock-heading">See the whole car</h2>
      {zones.length > 0 && (
        <>
          <ul className="lock-list" aria-label="Locked zones">
            {zones.map((zone) => (
              <li key={zone.zone} data-testid={`locked-${zone.zone}`}>
                <span><LockIcon />{zoneTitle(zone.zone)}</span>
                <span>{zone.finding_count} {zone.finding_count === 1 ? 'finding' : 'findings'}</span>
              </li>
            ))}
          </ul>
          <p className="res-note">Totals only. What each finding is, with its evidence frame, and how well each zone was covered are in the full report.</p>
        </>
      )}
      <p className="res-note">The full report also adds the listing check, repair cost ranges and a checklist for the seller and a mechanic.</p>
      {unlock && (
        <div className="stack-sm">
          <Button block disabled={unlock.busy} onClick={unlock.onUnlock}>
            {unlock.busy ? 'Opening checkout…' : 'Unlock full report · $19'}
          </Button>
          <p className="hint hint-center">One-time payment through our payment provider. Returning from checkout unlocks nothing until the payment is verified.</p>
          {unlock.error && <ResultError error={unlock.error} />}
        </div>
      )}
    </Card>
  );
}

function LockIcon(): ReactElement {
  return (
    <svg role="img" aria-label="Locked" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

function formatSourceTime(ms: number): string {
  return `${Math.floor(ms / 60000).toString().padStart(2, '0')}:${Math.floor((ms % 60000) / 1000).toString().padStart(2, '0')}.${Math.floor(ms % 1000).toString().padStart(3, '0')}`;
}
