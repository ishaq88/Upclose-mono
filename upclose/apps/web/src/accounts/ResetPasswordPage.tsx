import { useEffect, useId, useState, type FormEvent, type ReactElement } from 'react';
import { useAccount } from './AccountContext';
import { accountErrorMessage, MIN_PASSWORD, passwordProblem } from './errors';
import './accounts.css';

// Held in memory so a StrictMode double effect (or re-mount) still finds the token after the hash is stripped.
let heldToken: string | null = null;

function takeToken(): string | null {
  if (!window.location.hash) return heldToken;
  const match = /(?:^#|&)token=([^&]+)/.exec(window.location.hash);
  if (window.location.hash) {
    // Strip the secret from the address bar and history entry immediately.
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  try { heldToken = match ? decodeURIComponent(match[1]) : null; } catch { heldToken = null; }
  return heldToken;
}

/** Confirm a password reset. The token arrives in the URL fragment and is held in memory only. */
export function ResetPasswordPage(): ReactElement {
  const { api } = useAccount();
  const id = useId();
  const [token, setToken] = useState<string | null | undefined>(undefined);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [email, setEmail] = useState('');
  const [sent, setSent] = useState(false);

  useEffect(() => { setToken(takeToken()); }, []);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy || !token) return;
    const problem = passwordProblem(password);
    if (problem) { setError(problem); return; }
    setBusy(true);
    setError(null);
    try {
      await api.confirmPasswordReset(token, password);
      heldToken = null;
      setToken(null);
      setDone(true);
    } catch (e) {
      const code = (e as { code?: string }).code;
      if (code === 'TOKEN_INVALID' || code === 'NOT_FOUND') { heldToken = null; setInvalid(true); setToken(null); } else setError(accountErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function resend(event: FormEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    try { await api.requestPasswordReset(email.trim()); setSent(true); setError(null); } catch (e) { setError(accountErrorMessage(e)); } finally { setBusy(false); }
  }

  if (token === undefined) return <main className="wrap app-main" />;

  return (
    <main className="wrap app-main">
      <div className="acct-narrow">
        <h1 className="page-title">{done ? 'Password changed' : 'New password'}</h1>
        <div className="card acct-card" style={{ marginTop: 24 }}>
          {done ? (
            <>
              <p className="notice notice-ok" role="status">Your password is changed and every device has been signed out.</p>
              <a className="btn" href="/signin">Sign in</a>
            </>
          ) : token ? (
            <form className="acct-form" onSubmit={(e) => { void submit(e); }}>
              <div className="field">
                <label htmlFor={`${id}-pw`}>New password</label>
                <input id={`${id}-pw`} type="password" autoComplete="new-password" required aria-describedby={`${id}-hint`}
                  value={password} onChange={(e) => setPassword(e.target.value)} />
                <span className="hint" id={`${id}-hint`}>At least {MIN_PASSWORD} characters</span>
              </div>
              <div aria-live="polite">{error && <p className="notice notice-error" role="alert">{error}</p>}</div>
              <button className="btn" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Set new password'}</button>
            </form>
          ) : (
            <form className="acct-form" onSubmit={(e) => { void resend(e); }}>
              <p className="notice notice-error" role="alert">{invalid ? 'That reset link is invalid or has expired.' : 'This page needs a reset link.'} Request a new one below.</p>
              <div className="field">
                <label htmlFor={`${id}-email`}>Email</label>
                <input id={`${id}-email`} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
              </div>
              <div aria-live="polite">
                {sent && <p className="notice notice-ok">If that email has an account, we've sent a link to reset the password.</p>}
                {error && <p className="notice notice-error" role="alert">{error}</p>}
              </div>
              {!sent && <button className="btn" type="submit" disabled={busy}>{busy ? 'Sending…' : 'Send reset link'}</button>}
              <a className="textlink" href="/signin">Back to sign in</a>
            </form>
          )}
        </div>
      </div>
    </main>
  );
}
