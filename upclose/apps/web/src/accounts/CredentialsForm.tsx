import { useId, useState, type FormEvent, type ReactElement } from 'react';
import { accountErrorMessage, MIN_PASSWORD, passwordProblem } from './errors';
import { Button, Field, Notice } from '../ui';
import './accounts.css';

export interface CredentialsFormProps {
  mode: 'signin' | 'register';
  submitLabel: string;
  /** Resolve on success; throw to show an error. */
  onSubmit: (email: string, password: string) => Promise<void>;
}

/** Email + password form shared by the sign-in page and the save sheet. */
export function CredentialsForm({ mode, submitLabel, onSubmit }: CredentialsFormProps): ReactElement {
  const id = useId();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const register = mode === 'register';

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (busy) return;
    const problem = register ? passwordProblem(password) : null;
    if (problem) { setError(problem); return; }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(email.trim(), password);
    } catch (e) {
      setError(accountErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="acct-form" onSubmit={(e) => { void submit(e); }}>
      <Field label="Email">
        <input id={`${id}-email`} type="email" name="email" autoComplete={register ? 'email' : 'username'} required
          value={email} onChange={(e) => setEmail(e.target.value)} />
      </Field>
      <Field label="Password" hint={register ? `At least ${MIN_PASSWORD} characters` : undefined}>
        <input id={`${id}-pw`} type="password" name="password" autoComplete={register ? 'new-password' : 'current-password'} required
          value={password} onChange={(e) => setPassword(e.target.value)} />
      </Field>
      <div aria-live="polite">{error && <Notice tone="error">{error}</Notice>}</div>
      <Button type="submit" disabled={busy}>{busy ? 'One moment…' : submitLabel}</Button>
    </form>
  );
}
