import { lazy, Suspense, useCallback, useMemo, useRef, useState, type ReactElement } from 'react';
import { AccountProvider, useAccount } from './accounts/AccountContext';
import { createApi } from './api/client';
import { CheckoutReturn } from './components/CheckoutReturn';
import { CaptureFlow } from './components/CaptureFlow';
import { ConsentForm } from './components/ConsentForm';
import { clearSession, loadSession, saveSession, type StoredSession } from './session/storage';
import { AppHeader } from './ui/AppHeader';

// Screens off the capture path load on demand.
const SignInPage = lazy(() => import('./accounts/SignInPage').then((m) => ({ default: m.SignInPage })));
const ResetPasswordPage = lazy(() => import('./accounts/ResetPasswordPage').then((m) => ({ default: m.ResetPasswordPage })));
const MyReportsPage = lazy(() => import('./accounts/MyReportsPage').then((m) => ({ default: m.MyReportsPage })));
const ReportPage = lazy(() => import('./report/ReportPage').then((m) => ({ default: m.ReportPage })));

/** Path-based switch; no router dependency. `pathname` is injectable for tests. */
export interface AppProps {
  pathname?: string;
}

export function App({ pathname }: AppProps = {}): ReactElement {
  return (
    <AccountProvider>
      <Screens pathname={pathname ?? window.location.pathname} />
    </AccountProvider>
  );
}

function Screens({ pathname }: { pathname: string }): ReactElement {
  const path = pathname.replace(/\/+$/, '') || '/';
  const { account } = useAccount();
  const [stored, setStored] = useState<StoredSession | null>(loadSession);
  const tokenRef = useRef<string | null>(stored?.token ?? null);
  const accountRef = useRef<string | null>(account?.token ?? null);
  accountRef.current = account?.token ?? null;
  const api = useMemo(() => createApi({ getToken: () => tokenRef.current }), []);
  // Reports opened from My reports are read with the account token; owner flows keep the owner token.
  const reportApi = useMemo(() => createApi({ getToken: () => tokenRef.current ?? accountRef.current }), []);

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
  if (path === '/checkout/return') screen = <main className="wrap app-main"><CheckoutReturn /></main>;
  else if (path === '/signin') screen = <SignInPage />;
  else if (path === '/reset-password') screen = <ResetPasswordPage />;
  else if (path === '/reports') screen = <MyReportsPage />;
  else if (report) screen = <ReportPage slug={decodeURIComponent(report[1]!)} api={reportApi} />;
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
      <AppHeader />
      <div id="main">
        <Suspense fallback={<main className="wrap app-main"><p role="status" className="muted">Loading…</p></main>}>{screen}</Suspense>
      </div>
    </div>
  );
}
