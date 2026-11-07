import type { ReactElement } from 'react';
import type { Api, PartSummary, ProgressResponse } from '../api/client';
import { zoneTitle } from '../progress/labels';
import { describeReason, isQualityReason } from '../progress/reasons';
import { orderedParts, stagesFor } from '../progress/stages';
import { OPTIONAL_ZONES } from '../report/model';
import { defaultNavigate, isSafeCheckoutUrl } from '../results/checkout';
import { ResultError } from '../results/ResultError';
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
}

/** Server-reported processing state and authenticated owner report access. */
export function ProgressPanel({ api, progress, onRefresh, onUnauthorized, navigate = defaultNavigate }: ProgressPanelProps): ReactElement {
  return (
    <section aria-labelledby="progress-heading" className="res-col">
      <h2 id="progress-heading" style={{ margin: 0, fontSize: 28, fontWeight: 800, lineHeight: 1.05 }}>Processing and report</h2>
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
  if (!progress) return <p role="status" className="muted">Processing status has not been loaded yet. A missing status is not a clean result.</p>;
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
  switch (part.state) {
    case 'processed': return <span className="pill p-ok">Done</span>;
    case 'processing':
    case 'uploaded': return <span className="pill p-run"><span className="spin" aria-hidden="true" />Checking</span>;
    case 'uploading': return <span className="pill p-run"><span className="spin" aria-hidden="true" />Uploading</span>;
    case 'skipped': return <span className="pill p-not">Skipped</span>;
    case 'failed': return isQualityReason(part.reason)
      ? <span className="pill p-warn">Too blurry</span>
      : <span className="pill p-bad">Could not check</span>;
    case 'pending': return <span className="pill p-wait">{capturing ? 'Next' : 'Not filmed'}</span>;
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
  else if (progress.state === 'capturing') body = <p className="muted">Your report is created when you finish capture. Nothing is built until you choose Finish.</p>;
  else if (progress.report === null) body = <p role="status" className="muted">Capture is finished. Your report is being prepared; this page keeps checking.</p>;
  else if (progress.report.state === 'building') body = <p role="status" className="muted">Your report is being built. Zones are still processing in the background; this page keeps checking.</p>;
  else if (progress.report.state === 'failed') {
    body = (
      <p role="alert" className="notice notice-error">
        The report could not be built, so there is nothing to open or unlock. Zones listed as failed or skipped were not assessed, which is not a clean result.
      </p>
    );
  } else body = <ReadyReport {...props} slug={progress.report.slug} unlocked={progress.report.unlocked} />;
  return <div className="res-card" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>{body}</div>;
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
      <p role="status" style={{ fontWeight: 700 }}>{unlocked ? 'Your report is ready and unlocked.' : 'Your report is ready.'}</p>
      <div className="res-actions" style={{ marginTop: 0 }}>
        <button type="button" className="btn btn-sm btn-ghost" onClick={onRefresh}>Refresh report status</button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={open}>{unlocked ? 'Open your full report' : 'Open your free report'}</button>
        {!unlocked && <button type="button" className="btn btn-sm" disabled={unlock.busy} onClick={unlock.start}>
          {unlock.busy ? 'Opening checkout…' : 'Unlock full report · $19'}
        </button>}
      </div>
      {unlock.error && <ResultError error={unlock.error}><button type="button" className="textlink" onClick={unlock.dismiss}>Dismiss</button></ResultError>}
      <p className="res-note">
        Payment is handled by our payment provider. Returning from checkout does not unlock anything by itself; the report unlocks only after the server verifies the payment.
      </p>
    </>
  );
}
