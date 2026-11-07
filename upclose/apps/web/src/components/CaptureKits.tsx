import { useState, type ReactElement } from 'react';
import { PROVENANCE_LABEL, rigCommand, type KitRow } from '../capture/kits';

interface Props {
  rows: readonly KitRow[];
  sessionId: string;
  /** Read at click time and written only to the clipboard; it is never rendered. */
  ownerToken: string;
  capturing: boolean;
}

type CopyState = 'idle' | 'copied' | 'failed';

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function CaptureKits({ rows, sessionId, ownerToken, capturing }: Props): ReactElement {
  const [idState, setIdState] = useState<CopyState>('idle');
  const [commandState, setCommandState] = useState<CopyState>('idle');
  const [tokenState, setTokenState] = useState<CopyState>('idle');
  const command = rigCommand(sessionId);

  const copy = async (text: string, set: (state: CopyState) => void): Promise<void> => {
    set((await copyToClipboard(text)) ? 'copied' : 'failed');
  };
  const label = (state: CopyState, idle: string): string => (state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : idle);

  return (
    <section aria-labelledby="kits-heading" className="card kits">
      <h2 id="kits-heading">Capture kits</h2>
      <p className="hint">Extra evidence attached to this inspection. It does not change the condition score. Kits not attached show as Not checked.</p>
      <ul className="kit-list">
        {rows.map((row) => (
          <li key={row.id} className="kit-row" data-testid={`kit-${row.id}`}>
            <span className="kit-name">{row.label}</span>
            <span className="kit-status">
              <span className={row.attached ? 'tag' : 'tag tag-quiet'}>{row.attached ? '✓ Attached' : 'Not attached'}</span>
              {row.provenance && <span className="tag tag-quiet">{PROVENANCE_LABEL[row.provenance]}</span>}
              {row.notAssessed && <span className="tag tag-quiet">Not assessed</span>}
            </span>
          </li>
        ))}
      </ul>
      {capturing && (
        <details className="kit-connect">
          <summary>Connect the sensor pod</summary>
          <div className="stack">
            <div className="kit-field">
              <span className="lead-in">Session id</span>
              <code className="kit-code">{sessionId}</code>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void copy(sessionId, setIdState); }}>{label(idState, 'Copy session id')}</button>
            </div>
            <div className="kit-field">
              <span className="lead-in">Command</span>
              <code className="kit-code">{command}</code>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void copy(command, setCommandState); }}>{label(commandState, 'Copy command')}</button>
            </div>
            <p className="notice notice-warn" role="note">This key controls this inspection. Only paste it into your own rig. It is never shown again after you leave this page.</p>
            <div>
              <button type="button" className="btn btn-sm" onClick={() => { void copy(ownerToken, setTokenState); }}>{label(tokenState, 'Copy owner token')}</button>
              <span role="status" className="sr-only">{tokenState === 'copied' ? 'Owner token copied to the clipboard' : tokenState === 'failed' ? 'Could not copy the owner token' : ''}</span>
            </div>
          </div>
        </details>
      )}
    </section>
  );
}

export function MissingKitsNotice({ rows }: { rows: readonly KitRow[] }): ReactElement | null {
  if (rows.length === 0) return null;
  return (
    <p className="notice notice-warn" role="status" data-testid="missing-kits">
      <span className="lead-in">Not attached: </span>{rows.map((row) => row.label).join(', ')}. These will appear as Not checked in your report.
    </p>
  );
}
