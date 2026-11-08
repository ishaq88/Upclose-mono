import { lazy, Suspense, useCallback, useMemo, useRef, useState, type ReactElement } from 'react';
import { AccountProvider, useAccount } from './accounts/AccountContext';
import { createApi } from './api/client';
import { CheckoutReturn } from './components/CheckoutReturn';
import { CaptureFlow } from './components/CaptureFlow';
import { ConsentForm } from './components/ConsentForm';
import { clearSession, loadSession, saveSession, type StoredSession } from './session/storage';
import { ConnectedHeader, HeaderSlotProvider } from './capture/headerSlot';

// Screens off the capture path load on demand.
const SignInPage = lazy(() => import('./accounts/SignInPage').then((m) => ({ default: m.SignInPage })));
const ResetPasswordPage = lazy(() => import('./accounts/ResetPasswordPage').then((m) => ({ default: m.ResetPasswordPage })));
const MyReportsPage = lazy(() => import('./accounts/MyReportsPage').then((m) => ({ default: m.MyReportsPage })));
// Development only: the kit gallery. The import sits inside the DEV branch, so production builds drop it.
const Gallery = import.meta.env.DEV ? lazy(() => import('./ui/gallery/Gallery')) : null;
const ReportPage = lazy(() => import('./report/ReportPage').then((m) => ({ default: m.ReportPage })));

/** Path-based switch; no router dependency. `pathname` is injectable for tests. */
export interface AppProps {
  pathname?: string;
}

export function App({ pathname }: AppProps = {}): ReactElement {
  return (
    <AccountProvider>
      <HeaderSlotProvider>
        <Screens pathname={pathname ?? window.location.pathname} />
      </HeaderSlotProvider>
    </AccountProvider>
  );
}

function Screens({ pathname }: { pathname: string }): ReactElement {
  const path = pathname.replace(/\/+$/, '') || '/';
  const { account } = useAccount();
  const [stored, setStored] = useState<StoredSession | null>(loadSession);
  const tokenRef = useRef<string | null>(stored?.token ?? null);
  const api = useMemo(() => createApi({ getToken: () => tokenRef.current }), []);
  // A report is opened as its owner: the signed-in account first (saved reports belong to it on every device),
  // then this tab's capture session. ReportPage settles on one and uses it for the whole page.
  const accountToken = account?.token ?? null;
  const accountApi = useMemo(() => (accountToken ? createApi({ getToken: () => accountToken }) : null), [accountToken]);
  const reportCredentials = useMemo(() => ({ account: accountApi, owner: api }), [accountApi, api]);

  const onCreated = useCallback((session: StoredSession) => {
    tokenRef.current = session.token;
    saveSession(session);
    setStored(session);
  }, []);

  const onNewSession = useCallback(() => {
    clearSession();
    tokenRef.current = null;
    setStored(null);
  }, []);

  let screen: ReactElement;
  const report = /^\/report\/([^/]+)$/.exec(path);
  if (import.meta.env.DEV && Gallery && path === '/__gallery') screen = <Gallery />;
  else if (path === '/checkout/return') screen = <main className="wrap app-main"><CheckoutReturn /></main>;
  else if (path === '/signin') screen = <SignInPage />;
  else if (path === '/reset-password') screen = <ResetPasswordPage />;
  else if (path === '/reports') screen = <MyReportsPage />;
  else if (report) screen = <ReportPage slug={decodeURIComponent(report[1]!)} credentials={reportCredentials} />;
  else screen = (
    <main className="wrap app-main">
      {stored
        ? <CaptureFlow key={stored.sessionId} api={api} sessionId={stored.sessionId} ownerToken={stored.token} onNewSession={onNewSession} />
        : <ConsentForm api={api} onCreated={onCreated} />}
    </main>
  );

  return (
    <div className="app">
      <a className="skip-link" href="#main">Skip to content</a>
      <ConnectedHeader />
      <div id="main">
        <Suspense fallback={<main className="wrap app-main"><p role="status" className="muted">Loading…</p></main>}>{screen}</Suspense>
      </div>
    </div>
  );
}
