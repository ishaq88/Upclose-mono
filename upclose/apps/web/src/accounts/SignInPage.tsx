import { useId, useState, type FormEvent, type ReactElement } from 'react';
import { useAccount } from './AccountContext';
import { CredentialsForm } from './CredentialsForm';
import { accountErrorMessage } from './errors';
import { nav } from './nav';
import './accounts.css';

type Tab = 'signin' | 'register';

/** Sign in, create an account, or request a password reset link. */
export function SignInPage(): ReactElement {
  const { api, signedIn } = useAccount();
  const [tab, setTab] = useState<Tab>('signin');
  const [forgot, setForgot] = useState(false);

  async function enter(email: string, password: string): Promise<void> {
    const auth = tab === 'register' ? await api.register(email, password) : await api.login(email, password);
    signedIn(auth);
    nav.go('/reports');
  }

  return (
    <main className="wrap app-main">
      <div className="acct-narrow">
        <h1 className="page-title">{forgot ? 'Reset password' : tab === 'register' ? 'Create account' : 'Sign in'}</h1>
        <p className="muted" style={{ margin: '12px 0 24px' }}>Keep your inspections together and open them on any device.</p>
        <div className="card acct-card">
          {forgot ? (
            <ForgotForm request={(email) => api.requestPasswordReset(email)} onBack={() => setForgot(false)} />
          ) : (
            <>
              <div className="acct-tabs" role="tablist" aria-label="Account">
                <button type="button" role="tab" id="tab-signin" aria-selected={tab === 'signin'} aria-controls="acct-panel" onClick={() => setTab('signin')}>Sign in</button>
                <button type="button" role="tab" id="tab-register" aria-selected={tab === 'register'} aria-controls="acct-panel" onClick={() => setTab('register')}>Create account</button>
              </div>
              <div role="tabpanel" id="acct-panel" aria-labelledby={`tab-${tab}`}>
                <CredentialsForm key={tab} mode={tab} submitLabel={tab === 'register' ? 'Create account' : 'Sign in'} onSubmit={enter} />
              </div>
              <div className="acct-links">
                {tab === 'signin' && <button type="button" className="textlink" onClick={() => setForgot(true)}>Forgot password?</button>}
                <a className="textlink" href="/">Continue without an account</a>
              </div>
            </>
          )}
        </div>
      </div>
    </main>
  );
}

function ForgotForm({ request, onBack }: { request: (email: string) => Promise<void>; onBack: () => void }): ReactElement {
  const id = useId();
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await request(email.trim());
      setSent(true);
    } catch (e) {
      setError(accountErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="acct-form" onSubmit={(e) => { void submit(e); }}>
      <div className="field">
        <label htmlFor={`${id}-email`}>Email</label>
        <input id={`${id}-email`} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </div>
      <div aria-live="polite">
        {sent && <p className="notice notice-ok">If that email has an account, we've sent a link to reset the password.</p>}
        {error && <p className="notice notice-error" role="alert">{error}</p>}
      </div>
      {!sent && <button className="btn" type="submit" disabled={busy}>{busy ? 'Sending…' : 'Send reset link'}</button>}
      <button type="button" className="textlink" onClick={onBack}>Back to sign in</button>
    </form>
  );
}
