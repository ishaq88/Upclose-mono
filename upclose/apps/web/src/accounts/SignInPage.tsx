import { useId, useState, type FormEvent, type ReactElement } from 'react';
import { useAccount } from './AccountContext';
import { CredentialsForm } from './CredentialsForm';
import { accountErrorMessage } from './errors';
import { nav } from './nav';
import { Button, Card, Field, Notice } from '../ui';
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
        <p className="muted acct-lede">Keep your inspections together and open them on any device.</p>
        <Card padding="lg" className="acct-card">
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
                {tab === 'signin' && <Button variant="text" onClick={() => setForgot(true)}>Forgot password?</Button>}
                <Button variant="text" href="/">Continue without an account</Button>
              </div>
            </>
          )}
        </Card>
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
      <Field label="Email">
        <input id={`${id}-email`} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </Field>
      <div aria-live="polite">
        {sent && <Notice tone="ok" role="status">If that email has an account, we've sent a link to reset the password.</Notice>}
        {error && <Notice tone="error">{error}</Notice>}
      </div>
      {!sent && <Button type="submit" disabled={busy}>{busy ? 'Sending…' : 'Send reset link'}</Button>}
      <Button variant="text" onClick={onBack}>Back to sign in</Button>
    </form>
  );
}
