import { useRef, useState, type ReactElement } from 'react';
import type { Api } from '../api/client';
import { describeError, type UiError } from '../errors';
import { Button } from '../ui';
import { ResultError } from './ResultError';

/** Copy with the clipboard API; fall back to selecting the field and execCommand. Returns whether it worked. */
export async function copyText(text: string, field: HTMLInputElement | null): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission denied or insecure context: use the fallback.
  }
  if (!field) return false;
  field.focus();
  field.select();
  try {
    return document.execCommand('copy');
  } catch {
    return false;
  }
}

/** Owner-only read-only link for a mechanic. The link is the public report path; it carries no token. */
export function ShareControl({ api, slug }: { api: Api; slug: string }): ReactElement {
  const [link, setLink] = useState<string | null>(null);
  const [active, setActive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<UiError | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const field = useRef<HTMLInputElement>(null);

  const fail = (caught: unknown): void => {
    const described = describeError(caught);
    const status = (caught as { status?: number }).status;
    setError(status === 403 || status === 401
      ? { title: 'Only the owner can share this report', message: 'Open it from the browser tab where you filmed it, or sign in to the account that saved it.' }
      : described);
  };

  const enable = async (): Promise<void> => {
    setBusy(true); setError(null); setNote(null);
    try {
      const result = await api.enableShare(slug);
      setLink(`${window.location.origin}${result.url}`);
      setActive(result.public_access_active);
    } catch (caught) { fail(caught); } finally { setBusy(false); }
  };
  const disable = async (): Promise<void> => {
    setBusy(true); setError(null); setNote(null);
    try {
      await api.disableShare(slug);
      setLink(null);
      setNote('Sharing is off. The link no longer opens.');
    } catch (caught) { fail(caught); } finally { setBusy(false); }
  };
  const copy = async (): Promise<void> => {
    setNote((await copyText(link ?? '', field.current)) ? 'Link copied.' : 'Could not copy automatically. The link is selected: use your device’s copy command.');
  };

  return (
    <div className={link === null ? 'share-bar' : 'share-bar share-bar-open'} data-testid="share-control">
      {link === null ? (
        <Button variant="secondary" size="sm" disabled={busy} onClick={() => { void enable(); }}>
          {busy ? 'Turning on…' : 'Share'}
        </Button>
      ) : (
        <>
          <input ref={field} readOnly value={link} aria-label="Share link" onFocus={(e) => e.currentTarget.select()} />
          <Button size="sm" onClick={() => { void copy(); }}>Copy link</Button>
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => { void disable(); }}>Stop sharing</Button>
        </>
      )}
      {(link !== null || note) && (
        <p className="res-note share-note" role="status">
          {link !== null && !active ? 'The link is saved, but public sharing is switched off right now, so it will not open yet. ' : ''}
          {note ?? 'Anyone with this link can read the report. Your mechanic gets a read-only link; you can turn it off any time.'}
        </p>
      )}
      {error && <ResultError error={error} />}
    </div>
  );
}
