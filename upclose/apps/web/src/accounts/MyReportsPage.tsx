import { useEffect, useId, useState, type FormEvent, type ReactElement } from 'react';
import { loadSession } from '../session/storage';
import { useAccount } from './AccountContext';
import type { AccountSessionSummary } from './api';
import { accountErrorMessage, MIN_PASSWORD, passwordProblem } from './errors';
import './accounts.css';

type Load = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; rows: AccountSessionSummary[] };

interface RowView {
  title: string;
  status: string;
  /** Where the whole row goes; null when it cannot be opened from here. */
  href: string | null;
}

const dateFormat = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

function formatDate(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? '' : dateFormat.format(new Date(t));
}

/** Free-safe row model: only labels, states and counts, never findings or scores. */
export function describeRow(row: AccountSessionSummary, localSessionId: string | null): RowView {
  const date = formatDate(row.created_at);
  const title = row.vehicle_label?.trim() || (date ? `Inspection, ${date}` : 'Inspection');
  if (row.state === 'deleted') return { title, status: 'Deleted', href: null };
  if (row.state === 'deleting') return { title, status: 'Deleting…', href: null };
  if (row.report_state === 'ready' && row.report_slug) return { title, status: 'Report ready', href: `/report/${encodeURIComponent(row.report_slug)}` };
  if (row.report_state === 'failed') return { title, status: "Report couldn't be built", href: null };
  if (row.report_state === 'building' || row.state === 'finalized') return { title, status: 'Building your report', href: row.report_slug ? `/report/${encodeURIComponent(row.report_slug)}` : null };
  const counts = row.part_state_counts ?? {};
  const done = (counts.uploaded ?? 0) + (counts.processing ?? 0) + (counts.processed ?? 0);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const how = row.capture_mode === 'single_video' ? 'Video capture' : total > 0 ? `${done} of ${total} photos` : 'Guided capture';
  if (localSessionId === row.session_id) return { title, status: `${how}, in progress. Continue capturing`, href: '/' };
  return { title, status: `${how}, in progress. Continue on the device you started on`, href: null };
}

function Chevron(): ReactElement {
  return (
    <svg className="rep-chev" viewBox="0 0 20 20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 4l6 6-6 6" />
    </svg>
  );
}

/** The signed-in account's inspections, plus account actions. */
export function MyReportsPage(): ReactElement {
  const { account, api, signOut } = useAccount();
  const [load, setLoad] = useState<Load>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const signedIn = account !== null;

  useEffect(() => {
    if (!signedIn) return;
    let live = true;
    setLoad({ status: 'loading' });
    api.sessions().then(
      (rows) => { if (live) setLoad({ status: 'ready', rows }); },
      (e: unknown) => { if (live) setLoad({ status: 'error', message: accountErrorMessage(e) }); },
    );
    return () => { live = false; };
  }, [api, signedIn, attempt]);

  if (!account) {
    return (
      <main className="wrap app-main">
        <div className="acct-narrow">
          <h1 className="page-title">My reports</h1>
          <div className="card acct-card" style={{ marginTop: 24 }}>
            <p>Sign in to see the inspections you've saved.</p>
            <a className="btn" href="/signin">Sign in</a>
            <a className="textlink" href="/">Continue without an account</a>
          </div>
        </div>
      </main>
    );
  }

  const localId = loadSession()?.sessionId ?? null;

  return (
    <main className="wrap app-main">
      <div className="acct-head">
        <h1 className="page-title">My reports</h1>
        <a className="btn" href="/">New inspection</a>
      </div>
      <div className="acct-layout">
        <section aria-label="Your inspections" aria-busy={load.status === 'loading'}>
          {load.status === 'loading' && <p className="muted" role="status">Loading your reports…</p>}
          {load.status === 'error' && (
            <div className="notice notice-error" role="alert">
              <p>{load.message}</p>
              <button type="button" className="btn btn-ghost btn-sm" style={{ marginTop: 12 }} onClick={() => setAttempt((n) => n + 1)}>Try again</button>
            </div>
          )}
          {load.status === 'ready' && load.rows.length === 0 && (
            <div className="card acct-card">
              <h2>No saved inspections yet</h2>
              <p className="muted">After you inspect a car, choose "Save to my account" and it will show up here.</p>
            </div>
          )}
          {load.status === 'ready' && load.rows.length > 0 && (
            <ul className="rep-list">
              {load.rows.map((row) => {
                const view = describeRow(row, localId);
                const body = (
                  <>
                    <span className="rep-row-body">
                      <span className="rep-row-title">{view.title}</span>
                      <span className="rep-row-status">{view.status}</span>
                    </span>
                    {view.href && <><span className="rep-row-go">Open</span><Chevron /></>}
                  </>
                );
                return (
                  <li key={row.session_id}>
                    {view.href ? <a className="rep-row" href={view.href}>{body}</a> : <div className="rep-row rep-row-static">{body}</div>}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
        <aside className="card acct-side" aria-label="Account">
          <div className="acct-who">
            <span className="avatar" aria-hidden="true">{account.email.slice(0, 1).toUpperCase()}</span>
            <span>{account.email}</span>
          </div>
          <details className="acct-details">
            <summary>Change password</summary>
            <ChangePassword change={(a, b) => api.changePassword(a, b)} />
          </details>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void signOut(); }}>Sign out</button>
        </aside>
      </div>
    </main>
  );
}

function ChangePassword({ change }: { change: (current: string, next: string) => Promise<void> }): ReactElement {
  const id = useId();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy) return;
    const problem = passwordProblem(next);
    if (problem) { setError(problem); setDone(false); return; }
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await change(current, next);
      setCurrent('');
      setNext('');
      setDone(true);
    } catch (e) {
      setError(accountErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="acct-form" onSubmit={(e) => { void submit(e); }}>
      <div className="field">
        <label htmlFor={`${id}-cur`}>Current password</label>
        <input id={`${id}-cur`} type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor={`${id}-new`}>New password</label>
        <input id={`${id}-new`} type="password" autoComplete="new-password" required aria-describedby={`${id}-hint`} value={next} onChange={(e) => setNext(e.target.value)} />
        <span className="hint" id={`${id}-hint`}>At least {MIN_PASSWORD} characters. Other devices will be signed out.</span>
      </div>
      <div aria-live="polite">
        {done && <p className="notice notice-ok">Password changed. Other devices were signed out.</p>}
        {error && <p className="notice notice-error" role="alert">{error}</p>}
      </div>
      <button type="submit" className="btn btn-dark btn-sm" disabled={busy}>{busy ? 'Saving…' : 'Change password'}</button>
    </form>
  );
}
