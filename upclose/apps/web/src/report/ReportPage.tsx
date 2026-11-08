import type { ReactElement } from 'react';
import type { Api } from '../api/client';
import { OwnerReport } from '../components/OwnerReport';
import { loadSession } from '../session/storage';
import '../results/results.css';

const noop = (): void => {};

/**
 * The owner/account report at /report/<slug>. `api` carries the owner token, or the account token for
 * reports opened from My reports; either goes only in an Authorization header.
 */
export function ReportPage({ slug, api }: { slug: string; api: Api }): ReactElement {
  return (
    <main className="wrap app-main">
      <OwnerReport api={api} slug={slug} onUnauthorized={noop} onRefresh={noop} sessionId={loadSession()?.sessionId} />
    </main>
  );
}
