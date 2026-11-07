import { useEffect, useRef, useState, type ReactElement } from 'react';
import { useAccount } from './AccountContext';
import { CredentialsForm } from './CredentialsForm';
import { accountErrorMessage } from './errors';
import './accounts.css';

export interface SaveInspectionProps {
  sessionId: string;
  /** The inspection's owner token: needed once to link it to the account, never stored by this component. */
  ownerToken: string;
}

type Mode = 'register' | 'signin';

/** Prompt to save (claim) the current inspection to an account, in a sheet (phone) or dialog (desktop). */
export function SaveInspection({ sessionId, ownerToken }: SaveInspectionProps): ReactElement {
  const { account, api, signedIn } = useAccount();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const [mode, setMode] = useState<Mode>('register');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    } else if (!open && dialog.open) {
      if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
    }
  }, [open]);

  function close(): void {
    setOpen(false);
    openerRef.current?.focus();
  }

  async function claim(): Promise<void> {
    await api.claim(sessionId, ownerToken);
    setSaved(true);
    setError(null);
  }

  async function saveSignedIn(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    try { await claim(); } catch (e) { setError(accountErrorMessage(e)); } finally { setBusy(false); }
  }

  async function enter(email: string, password: string): Promise<void> {
    const auth = mode === 'register' ? await api.register(email, password) : await api.login(email, password);
    signedIn(auth);
    // The form unmounts once signed in, so a failed claim is reported by the signed-in view.
    try { await claim(); } catch (e) { setError(accountErrorMessage(e)); }
  }

  if (saved) {
    return (
      <div className="save-card" role="status">
        <div>
          <h2>Saved</h2>
          <p className="muted">It's in <a href="/reports">My reports</a>.</p>
        </div>
      </div>
    );
  }

  return (
    <section className="save-card" aria-labelledby="save-title">
      <div>
        <h2 id="save-title">Save this inspection to your account</h2>
        <p className="muted">Open it again on any device.</p>
      </div>
      <button ref={openerRef} type="button" className="btn btn-dark btn-sm" onClick={() => { setError(null); setOpen(true); }}>Save</button>
      <dialog ref={dialogRef} className="save-dialog" aria-labelledby="save-dialog-title" onCancel={(e) => { e.preventDefault(); close(); }} onClose={() => setOpen(false)}>
        {open && (
          <div className="save-dialog-body">
            <div className="save-dialog-head">
              <h2 id="save-dialog-title">{account ? 'Save to my account' : mode === 'register' ? 'Create an account to save it' : 'Sign in to save it'}</h2>
              <button type="button" className="btn btn-ghost btn-sm" onClick={close} aria-label="Close">Close</button>
            </div>
            {account ? (
              <>
                <p className="muted">Saving as {account.email}.</p>
                <div aria-live="polite">{error && <p className="notice notice-error" role="alert">{error}</p>}</div>
                <button type="button" className="btn" disabled={busy} onClick={() => { void saveSignedIn(); }} autoFocus>{busy ? 'Saving…' : 'Save to my account'}</button>
              </>
            ) : (
              <>
                <CredentialsForm key={mode} mode={mode} submitLabel={mode === 'register' ? 'Create account and save' : 'Sign in and save'} onSubmit={enter} />
                <button type="button" className="textlink" onClick={() => setMode(mode === 'register' ? 'signin' : 'register')}>
                  {mode === 'register' ? 'I already have an account' : 'Create an account instead'}
                </button>
              </>
            )}
          </div>
        )}
      </dialog>
    </section>
  );
}
