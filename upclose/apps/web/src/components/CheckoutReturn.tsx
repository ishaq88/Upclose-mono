import { useEffect, useState, type ReactElement } from 'react';
import { clearPendingCheckout, defaultNavigate, pendingCheckout } from '../results/checkout';
import '../results/results.css';

export interface CheckoutReturnProps {
  /** Injectable for tests; defaults to a full-page navigation. */
  navigate?: (url: string) => void;
  redirectMs?: number;
}

/**
 * Where the payment provider sends the browser afterwards. This page reads nothing from the address (no
 * `result=success` or any other query value) and calls no API: browser navigation is not payment evidence.
 * It only routes back, to the report the user sent to checkout (remembered in this tab) or to their
 * session. The report page shows whatever the server has verified; payment state comes only from the
 * provider's verified notification to our server.
 */
export function CheckoutReturn({ navigate = defaultNavigate, redirectMs = 2500 }: CheckoutReturnProps): ReactElement {
  const [target] = useState(() => {
    const slug = pendingCheckout();
    return slug ? `/report/${encodeURIComponent(slug)}` : '/';
  });
  useEffect(() => {
    const timer = window.setTimeout(() => { navigate(target); }, redirectMs);
    return () => window.clearTimeout(timer);
  }, [navigate, redirectMs, target]);
  return (
    <section aria-labelledby="return-heading" className="res-card build-card" style={{ margin: '0 auto' }}>
      <h1 id="return-heading" className="page-title" style={{ fontSize: 'clamp(28px, 4vw, 36px)' }}>Checking your payment…</h1>
      <p role="status" className="muted">
        <span className="spin" aria-hidden="true" />
        Taking you back. Your full report opens once our payment provider confirms the payment to our server, which can take a short while. Opening this page does not unlock anything. If you cancelled, you can start again from your report.
      </p>
      <div className="res-actions">
        <a className="btn btn-sm" href={target} onClick={() => { if (target === '/') clearPendingCheckout(); }}>{target === '/' ? 'Back to your session' : 'Back to your report'}</a>
      </div>
    </section>
  );
}
