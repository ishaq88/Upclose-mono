import type { ReactElement } from 'react';
import type { Api, PartSummary, ProgressResponse } from '../api/client';
import { zoneTitle } from '../progress/labels';
import { describeReason, isQualityReason } from '../progress/reasons';
import { orderedParts, stagesFor } from '../progress/stages';
import { OPTIONAL_ZONES } from '../report/model';
import { defaultNavigate, isSafeCheckoutUrl } from '../results/checkout';
import { ResultError } from '../results/ResultError';
import { Button } from '../ui/Button';
import { Notice } from '../ui/Notice';
import { Pill, type PillTone } from '../ui/Pill';
import { cx } from '../ui/cx';
import { useUnlock } from '../results/useUnlock';
import '../results/results.css';

export { isSafeCheckoutUrl };

export interface ProgressPanelProps {
  api: Api;
  /** Latest owner progress from the single poll, or null before the first read. */
  progress: ProgressResponse | null;
  /** Re-read server state now (backs off internally if the service is limiting requests). */
  onRefresh: () => void;
  onUnauthorized: () => void;
  /** Injectable for tests; defaults to a full-page navigation. */
  navigate?: (url: string) => void;
  /** Rail mode for the capture page: stage bar, one-line summary, problem zones and the report state only. */
  compact?: boolean;
}

/** Server-reported processing state and authenticated owner report access. */
export function ProgressPanel({ api, progress, onRefresh, onUnauthorized, navigate = defaultNavigate, compact = false }: ProgressPanelProps): ReactElement {
  if (compact) {
    const parts = progress ? orderedParts(progress.parts) : [];
    const problems = parts.filter((p) => p.state === 'failed');
    return (
      <section aria-labelledby="progress-heading" className="rail">
        <h2 id="progress-heading" className="rail-title">Processing and report</h2>
        <Stages progress={progress} />
        <RailSummary progress={progress} />
        {problems.length > 0 && <ul className="zrows zrows-flat" aria-label="Zones that could not be checked">{problems.map((part) => <ZoneRow key={part.id} part={part} capturing={progress?.state === 'capturing'} />)}</ul>}
        <ReportStatus api={api} progress={progress} onRefresh={onRefresh} onUnauthorized={onUnauthorized} navigate={navigate} />
      </section>
    );
  }
  return (
    <section aria-labelledby="progress-heading" className="res-col">
      <h2 id="progress-heading" className="section-title">Processing and report</h2>
      <div className="prog">
        <div className="res-col">
          <Stages progress={progress} />
          <ZoneStatus progress={progress} />
        </div>
        <ReportStatus api={api} progress={progress} onRefresh={onRefresh} onUnauthorized={onUnauthorized} navigate={navigate} />
      </div>
    </section>
  );
}

function RailSummary({ progress }: { progress: ProgressResponse | null }): ReactElement {
  if (!progress) return <p role="status" className="muted rail-line">Loading status...</p>;
  const count = (states: string[]): number => progress.parts.filter((p) => states.includes(p.state)).length;
  const waiting = count(['uploaded']);
  const checking = count(['processing']);
  const done = count(['processed']);
  const failed = count(['failed']);
  const bits = [
    waiting > 0 ? `${waiting} queued` : '',
    checking > 0 ? `${checking} checking` : '',
    done > 0 ? `${done} done` : '',
    failed > 0 ? `${failed} could not be checked` : '',
  ].filter(Boolean);
  return (
    <>
      <p role="status" className="rail-line">{bits.length > 0 ? bits.join(' · ') : 'Nothing uploaded yet.'}</p>
      <p className="sr-only" role="status">{count(['processed', 'failed', 'skipped'])} of {progress.parts.length} zones finished.</p>
    </>
  );
}

function Stages({ progress }: { progress: ProgressResponse | null }): ReactElement | null {
  if (!progress) return null;
  return (
    <ol className="stages" aria-label="Progress">
      {stagesFor(progress).map((stage) => (
        <li key={stage.label} className={stage.status === 'done' ? 'on' : stage.status === 'now' ? 'now' : undefined} aria-current={stage.status === 'now' ? 'step' : undefined}>
          {stage.label}
          {stage.status === 'done' && <span className="sr-only"> (done)</span>}
        </li>
      ))}
    </ol>
  );
}

function ZoneStatus({ progress }: { progress: ProgressResponse | null }): ReactElement {
  if (!progress) return <p role="status" className="muted">Status not loaded yet. A missing status is not a clean result.</p>;
  const finished = progress.parts.filter((p) => p.state === 'processed' || p.state === 'failed' || p.state === 'skipped').length;
  return (
    <>
      <p className="sr-only" role="status">{finished} of {progress.parts.length} zones finished.</p>
      <ul className="zrows" aria-label="Processing status by zone">
        {orderedParts(progress.parts).map((part) => <ZoneRow key={part.id} part={part} capturing={progress.state === 'capturing'} />)}
      </ul>
    </>
  );
}

function ZoneRow({ part, capturing }: { part: PartSummary; capturing: boolean }): ReactElement {
  return (
    <li data-testid={`progress-part-${part.part_no}`}>
      <span className="zname">
        {zoneTitle(part.zone)}
        {OPTIONAL_ZONES.has(part.zone) && <span className="opt-tag">Optional</span>}
      </span>
      <ZonePill part={part} capturing={capturing} />
      {part.state === 'failed' && (
        <p className="zreason" data-testid={`progress-reason-${part.part_no}`}>
          {describeReason(part.reason)}
          {isQualityReason(part.reason) && ' Film this zone again in better light as a new inspection; it cannot be redone in this one.'}
        </p>
      )}
    </li>
  );
}

function ZonePill({ part, capturing }: { part: PartSummary; capturing: boolean }): ReactElement {
  const pill = (tone: PillTone, label: string, spin = false): ReactElement => (
    <Pill tone={tone} className={cx(spin && 'pill-run')}>{spin && <span className="spin" aria-hidden="true" />}{label}</Pill>
  );
  switch (part.state) {
    case 'processed': return pill('ok', 'Done');
    case 'processing': return pill('warn', 'Checking damage', true);
    case 'uploaded': return pill('optional', 'Queued');
    case 'uploading': return pill('warn', 'Uploading', true);
    case 'skipped': return pill('not', 'Skipped');
    case 'failed': return isQualityReason(part.reason) ? pill('warn', 'Too blurry') : pill('risk', 'Could not check');
    case 'pending': return pill('optional', capturing ? 'Next' : 'Not filmed');
  }
}

interface ReportStatusProps extends Pick<ProgressPanelProps, 'api' | 'onRefresh' | 'onUnauthorized'> {
  progress: ProgressResponse | null;
  navigate: (url: string) => void;
}

function ReportStatus(props: ReportStatusProps): ReactElement {
  const { progress } = props;
  let body: ReactElement;
  if (!progress) body = <p className="muted">The report state is not known yet.</p>;
  else if (progress.state === 'capturing') body = <p className="muted">The report is built when you finish capture.</p>;
  else if (progress.report === null) body = <p role="status" className="muted">Capture is finished. Preparing your report...</p>;
  else if (progress.report.state === 'building') body = <p role="status" className="muted">Your report is being built. Zones still processing finish in the background.</p>;
  else if (progress.report.state === 'failed') {
    body = (
      <Notice tone="error">
        <p>The report could not be built. Failed or skipped zones were not assessed, which is not a clean result.</p>
      </Notice>
    );
  } else body = <ReadyReport {...props} slug={progress.report.slug} unlocked={progress.report.unlocked} />;
  return <div className="res-card res-stack">{body}</div>;
}

function ReadyReport({ api, slug, unlocked, onRefresh, onUnauthorized, navigate }: ReportStatusProps & { slug: string; unlocked: boolean }): ReactElement {
  // The report opens on its own page, where it gets the full width; the owner token stays in this tab's storage.
  const open = (): void => navigate(`/report/${encodeURIComponent(slug)}`);
  const unlock = useUnlock({
    api, slug, navigate, onUnauthorized, onFailed: onRefresh,
    onUnlocked: open, // demo stand-in (DEMO_FREE_UNLOCK): the server already granted it
  });
  return (
    <>
      <p role="status" className="res-strong">{unlocked ? 'Your report is ready and unlocked.' : 'Your report is ready.'}</p>
      <div className="res-actions res-actions-flush">
        <Button variant="secondary" size="sm" onClick={onRefresh}>Refresh report status</Button>
        <Button variant="secondary" size="sm" onClick={open}>{unlocked ? 'Open your full report' : 'Open your free report'}</Button>
        {!unlocked && <Button size="sm" disabled={unlock.busy} onClick={unlock.start}>
          {unlock.busy ? 'Opening checkout…' : 'Unlock full report · $19'}
        </Button>}
      </div>
      {unlock.error && <ResultError error={unlock.error}><Button variant="text" size="sm" onClick={unlock.dismiss}>Dismiss</Button></ResultError>}
      <p className="res-note">
        Unlocks only after the payment is verified.
      </p>
    </>
  );
}
