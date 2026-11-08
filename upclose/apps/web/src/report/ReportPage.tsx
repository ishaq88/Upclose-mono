import { useEffect, useState, type ReactElement } from 'react';
import { ApiError, type Api } from '../api/client';
import { OwnerReport } from '../components/OwnerReport';
import '../results/results.css';

const noop = (): void => {};

/**
 * Credentials that may own a report, in the order the page tries them. A report saved to an account belongs to
 * that account wherever it is opened, so the account goes first; this tab's owner token covers a report that
 * was never saved. Each entry is null when that credential is absent.
 */
export interface ReportCredentials {
  /** Present only while signed in. */
  account: Api | null;
  /** Always present: carries this tab's owner token, or no token at all (slug-only access). */
  owner: Api;
}

type Resolution = { state: 'resolving' } | { state: 'resolved'; api: Api };

/** True when this credential does not own the report (another session's token, or an account it is not linked to). */
function notOwner(caught: unknown): boolean {
  return caught instanceof ApiError && (caught.status === 401 || caught.status === 404);
}

/**
 * The owner/account report at /report/<slug>. The page settles once on the credential that owns the report and
 * uses only that one for the report, its frames and its audio; a report is never bound to whichever capture
 * session this tab happens to hold.
 */
export function ReportPage({ slug, credentials }: { slug: string; credentials: ReportCredentials }): ReactElement {
  const [resolution, setResolution] = useState<Resolution>({ state: 'resolving' });
  const { account, owner } = credentials;

  useEffect(() => {
    let cancelled = false;
    const candidates = [account, owner].filter((api): api is Api => api !== null);
    void (async () => {
      for (const api of candidates.slice(0, -1)) {
        try {
          await api.getReport(slug);
          if (!cancelled) setResolution({ state: 'resolved', api });
          return;
        } catch (caught) {
          // Any other failure (network, building, server) says nothing about ownership: keep this credential
          // and let the report view show its own error and retry.
          if (!notOwner(caught)) {
            if (!cancelled) setResolution({ state: 'resolved', api });
            return;
          }
        }
      }
      // The last candidate (or the only one) renders directly; OwnerReport shows its own errors.
      const last = candidates[candidates.length - 1];
      if (!cancelled && last) setResolution({ state: 'resolved', api: last });
    })();
    return () => { cancelled = true; };
  }, [slug, account, owner]);

  if (resolution.state === 'resolving') {
    return <main className="wrap app-main"><p role="status" className="muted">Loading…</p></main>;
  }
  return (
    <main className="wrap app-main">
      <OwnerReport api={resolution.api} slug={slug} onUnauthorized={noop} onRefresh={noop} />
    </main>
  );
}
