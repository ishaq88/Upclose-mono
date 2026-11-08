import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { ApiError, type Api, type FreeReport, type PaidReport, type ReportStatus } from '../api/client';
import { SESSION_POLL_MS } from '../config';
import { describeError, type UiError } from '../errors';
import { nextPollDelay } from '../progress/polling';
import { ReportView, type ReportHeader } from '../report/ReportView';
import { clearPendingCheckout, pendingCheckout } from '../results/checkout';
import { isSyntheticPaid } from '../results/freeModel';
import { ResultError } from '../results/ResultError';
import { ShareControl } from '../results/ShareControl';
import { useUnlock } from '../results/useUnlock';
import { Button, Card, Notice, PageHeader, Pill } from '../ui';
import '../results/results.css';
import { FreeVerdictCard } from './FreeVerdictCard';
import { RenameLabel } from './RenameLabel';

type Loaded = FreeReport | PaidReport | ReportStatus;

/** Free and paid reports have different shapes; a status document has neither findings nor scores. */
function kindOf(report: Loaded): 'paid' | 'free' | 'status' {
  if ('scores' in report) return report.state === 'ready' ? 'paid' : 'status';
  if ('findings' in report) return 'free';
  return 'status';
}

/** How many extra reads to make after a checkout return while the server has not yet verified payment. */
const PAYMENT_CHECKS = 10;

export interface OwnerReportProps {
  api: Api;
  slug: string;
  onUnauthorized: () => void;
  onRefresh: () => void;
  /** Base poll interval for a report that is still building. */
  pollMs?: number;
  navigate?: (url: string) => void;
}

/**
 * The owner's report: JSON only (no server HTML). Paid reports render ReportView with evidence frames
 * fetched one by one as data URLs (token in a header). Free reports show the driver-side result with
 * unlock. A building report polls with backoff. Which of these shows is decided only by the server's response.
 */
export function OwnerReport({ api, slug, onUnauthorized, onRefresh, pollMs = SESSION_POLL_MS, navigate }: OwnerReportProps): ReactElement {
  const [report, setReport] = useState<Loaded | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const [fatal, setFatal] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // Remember a checkout return once, so a still-free report is re-read while the provider's confirmation arrives.
  const [awaiting, setAwaiting] = useState(() => pendingCheckout() === slug);
  const [gaveUp, setGaveUp] = useState(false);
  const [renamed, setRenamed] = useState<string | null | undefined>(undefined);
  const awaitingRef = useRef(awaiting);
  awaitingRef.current = awaiting;
  const frames = useRef<{ cache: Map<string, Promise<string>>; signal: AbortSignal | undefined }>({ cache: new Map(), signal: undefined });

  useEffect(() => {
    const controller = new AbortController();
    frames.current = { cache: new Map(), signal: controller.signal };
    let timer: number | undefined;
    let failures = 0;
    let waits = 0;
    setReport(null);
    setError(null);
    setFatal(false);
    setGaveUp(false);
    const schedule = (retryAfter: number | null): void => {
      timer = window.setTimeout(() => { void load(); }, nextPollDelay({ baseMs: pollMs, failures, retryAfterSeconds: retryAfter }));
    };
    const load = async (): Promise<void> => {
      try {
        const next = await api.getReport(slug);
        if (controller.signal.aborted) return;
        failures = 0;
        setError(null);
        setReport(next);
        const kind = kindOf(next);
        if (kind === 'paid') { clearPendingCheckout(); setAwaiting(false); return; }
        if (kind === 'status') { if (next.state === 'building') schedule(null); return; }
        if (awaitingRef.current) {
          waits += 1;
          if (waits < PAYMENT_CHECKS) schedule(null); else setGaveUp(true);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        if (caught instanceof ApiError && caught.code === 'REPORT_NOT_READY') {
          setError(null);
          setReport({ revision_id: '', scope: 'paid', slug, state: 'building' });
          schedule(caught.retryAfterSeconds);
          return;
        }
        setError(describeError(caught));
        if (caught instanceof ApiError && (caught.status === 401 || caught.status === 404)) {
          setFatal(true); // nothing to retry; only a 401 says the credential is refused
          if (caught.status === 401) onUnauthorized();
        } else if (failures < 5) {
          failures += 1;
          schedule(caught instanceof ApiError ? caught.retryAfterSeconds : null);
        }
        onRefresh();
      }
    };
    void load();
    return () => { controller.abort(); if (timer !== undefined) window.clearTimeout(timer); };
    // onUnauthorized/onRefresh are deliberately not dependencies: a new callback identity must not refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, slug, attempt, pollMs]);

  const resolveFrame = useCallback((id: string): Promise<string> => {
    const { cache, signal } = frames.current;
    let found = cache.get(id);
    if (!found) {
      found = api.getFrameDataUrl(`/api/v1/reports/${encodeURIComponent(slug)}/frames/${id}`, signal);
      cache.set(id, found);
      found.catch(() => { cache.delete(id); });
    }
    return found;
  }, [api, slug]);

  const unlock = useUnlock({
    api, slug, navigate, onUnauthorized,
    onUnlocked: () => setAttempt((n) => n + 1),
    onFailed: onRefresh,
  });

  const retry = (): void => { setAwaiting(false); setAttempt((n) => n + 1); };
  const label = renamed !== undefined ? renamed : report && 'vehicle_label' in report ? report.vehicle_label : null;
  const kind = report ? kindOf(report) : null;
  // The report names its own inspection for any owner credential (owner token or linked account); never fall
  // back to whichever inspection this tab happens to be capturing, which may be a different one.
  const renameSessionId = report && 'session_id' in report ? report.session_id ?? null : null;

  return (
    <div className="res-col" data-testid="owner-report">
      {kind !== 'paid' && (
        <PageHeader crumbs={[{ label: 'My reports', href: '/reports' }, { label: label ?? 'Report' }]} title={label ?? 'Your report'}
          actions={renameSessionId && report ? <RenameLabel api={api} sessionId={renameSessionId} label={label} onRenamed={setRenamed} /> : undefined} />
      )}
      {error && (
        <ResultError error={error}>
          {!fatal && <Button variant="secondary" size="sm" onClick={retry}>Retry report</Button>}
          {fatal && <Button variant="secondary" size="sm" href="/signin">Sign in</Button>}
          {fatal && <Button variant="text" size="sm" href="/reports">My reports</Button>}
        </ResultError>
      )}
      {report === null && !error && <p role="status" className="muted">Loading your report…</p>}
      {report && kind === 'status' && <Status state={report.state === 'failed' ? 'failed' : 'building'} />}
      {report && kind === 'free' && (
        <>
          {awaiting && !gaveUp && <Notice tone="warn" role="status">Confirming your payment with our payment provider. The full report opens only after the payment is verified; this page keeps checking.</Notice>}
          {awaiting && gaveUp && <Notice tone="warn" role="status">We have not received the payment confirmation yet. If you paid, it can take a few minutes. Reload this page to check again. Nothing is charged twice by waiting.</Notice>}
          <FreeVerdictCard data={report as FreeReport} layout="page" unlock={{ onUnlock: unlock.start, busy: unlock.busy, error: unlock.error }} />
        </>
      )}
      {report && kind === 'paid' && (
        <PaidBody report={report as PaidReport} slug={slug} api={api} resolveFrame={resolveFrame} label={label}
          rename={renameSessionId ? <RenameLabel api={api} sessionId={renameSessionId} label={label} onRenamed={setRenamed} /> : null} />
      )}
    </div>
  );
}

function Status({ state: value }: { state: 'building' | 'failed' }): ReactElement {
  if (value === 'failed') {
    return (
      <Notice tone="error" title="The report could not be built." className="build-card">
        There is nothing to open or unlock. Zones that failed or were skipped were not assessed, which is not a clean result. Start a new inspection to try again.
      </Notice>
    );
  }
  return (
    <Card padding="lg" className="build-card">
      <h2 className="build-title">Building your report</h2>
      <p role="status" className="muted"><span className="spin" aria-hidden="true" />This usually takes a few minutes. This page checks again by itself; you can leave it open.</p>
    </Card>
  );
}

function issuedOn(renderedAt: string | null): string | null {
  if (!renderedAt) return null;
  const date = new Date(renderedAt);
  return Number.isNaN(date.getTime()) ? null : `Issued ${date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`;
}

interface PaidBodyProps { report: PaidReport; slug: string; api: Api; resolveFrame: (id: string) => Promise<string>; label: string | null; rename: ReactNode }

function PaidBody({ report, slug, api, resolveFrame, label, rename }: PaidBodyProps): ReactElement {
  const synthetic = useMemo(() => isSyntheticPaid(report), [report]);
  const resolveMedia = useCallback((which: import('../api/client').ReportMedia, signal: AbortSignal) => api.getReportMedia(slug, which, signal), [api, slug]);
  const issued = issuedOn(report.rendered_at);
  const header: ReportHeader = {
    crumbs: [{ label: 'My reports', href: '/reports' }, { label: label ?? 'Report' }],
    title: label ?? 'Your report',
    meta: (
      <>
        {synthetic && <Pill tone="synthetic" data-testid="synthetic-badge">Synthetic test result. No real inference was performed.</Pill>}
        {issued && <span>{issued}</span>}
      </>
    ),
    actions: (
      <>
        <ShareControl api={api} slug={slug} />
        {report.narration_audio && <Button variant="secondary" size="sm" href="#r-advice">Listen</Button>}
        {rename}
      </>
    ),
  };
  return (
    <>
      <ReportView report={report} header={header} resolveFrame={resolveFrame} resolveMedia={resolveMedia} />
      <p className="res-note">Private report. Evidence frames load from your account only as you scroll. Anyone you share the link with can read the report; downloaded copies cannot be recalled.</p>
    </>
  );
}
