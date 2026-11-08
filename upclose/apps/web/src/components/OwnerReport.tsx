import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { ApiError, type Api, type FreeReport, type PaidReport, type ReportStatus } from '../api/client';
import { SESSION_POLL_MS } from '../config';
import { describeError, type UiError } from '../errors';
import { nextPollDelay } from '../progress/polling';
import { ReportView } from '../report/ReportView';
import { clearPendingCheckout, pendingCheckout } from '../results/checkout';
import { isSyntheticPaid } from '../results/freeModel';
import { ResultError } from '../results/ResultError';
import { ShareControl } from '../results/ShareControl';
import { useUnlock } from '../results/useUnlock';
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
  /** The owner's inspection id. When given, the title gets an inline Rename (owner token only). */
  sessionId?: string;
}

/**
 * The owner's report: JSON only (no server HTML). Paid reports render ReportView with evidence frames
 * fetched one by one as data URLs (token in a header). Free reports show the driver-side result with
 * unlock. A building report polls with backoff. Which of these shows is decided only by the server's response.
 */
export function OwnerReport({ api, slug, onUnauthorized, onRefresh, pollMs = SESSION_POLL_MS, navigate, sessionId }: OwnerReportProps): ReactElement {
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
          setFatal(true);
          onUnauthorized();
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

  return (
    <div className="res-col" data-testid="owner-report">
      <header className="res-head">
        <nav className="crumbs" aria-label="Breadcrumb"><a href="/reports">My reports</a><span aria-hidden="true">/</span><span>{label ?? 'Report'}</span></nav>
        <h1 className="page-title">{label ?? 'Your report'}</h1>
        {sessionId && report && <RenameLabel api={api} sessionId={sessionId} label={label} onRenamed={setRenamed} />}
      </header>
      {error && (
        <ResultError error={error}>
          {!fatal && <button type="button" className="btn btn-sm btn-ghost" onClick={retry}>Retry report</button>}
          {fatal && <a className="btn btn-sm btn-ghost" href="/signin">Sign in</a>}
          {fatal && <a className="textlink" href="/reports">My reports</a>}
        </ResultError>
      )}
      {report === null && !error && <p role="status" className="muted">Loading your report…</p>}
      {report && kind === 'status' && <Status state={report.state === 'failed' ? 'failed' : 'building'} />}
      {report && kind === 'free' && (
        <>
          {awaiting && !gaveUp && <p role="status" className="notice notice-warn">Confirming your payment with our payment provider. The full report opens only after the payment is verified; this page keeps checking.</p>}
          {awaiting && gaveUp && <p role="status" className="notice notice-warn">We have not received the payment confirmation yet. If you paid, it can take a few minutes. Reload this page to check again. Nothing is charged twice by waiting.</p>}
          <FreeVerdictCard data={report as FreeReport} layout="page" unlock={{ onUnlock: unlock.start, busy: unlock.busy, error: unlock.error }} />
        </>
      )}
      {report && kind === 'paid' && <PaidBody report={report as PaidReport} slug={slug} api={api} resolveFrame={resolveFrame} />}
    </div>
  );
}

function Status({ state: value }: { state: 'building' | 'failed' }): ReactElement {
  if (value === 'failed') {
    return (
      <div role="alert" className="notice notice-error build-card">
        <b>The report could not be built.</b>
        <span>There is nothing to open or unlock. Zones that failed or were skipped were not assessed, which is not a clean result. Start a new inspection to try again.</span>
      </div>
    );
  }
  return (
    <div className="res-card build-card">
      <h2 style={{ fontSize: 24, fontWeight: 800 }}>Building your report</h2>
      <p role="status" className="muted"><span className="spin" aria-hidden="true" />This usually takes a few minutes. This page checks again by itself; you can leave it open.</p>
    </div>
  );
}

function PaidBody({ report, slug, api, resolveFrame }: { report: PaidReport; slug: string; api: Api; resolveFrame: (id: string) => Promise<string> }): ReactElement {
  const synthetic = useMemo(() => isSyntheticPaid(report), [report]);
  return (
    <>
      {synthetic && <p className="res-badge" data-testid="synthetic-badge">Synthetic test result. No real inference was performed.</p>}
      <ShareControl api={api} slug={slug} />
      <p className="res-note">Private report. Evidence frames load from your account only as you scroll. Anyone you share the link with can read the report; downloaded copies cannot be recalled.</p>
      <ReportView report={report} resolveFrame={resolveFrame} />
    </>
  );
}
