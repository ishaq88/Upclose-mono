import { useState, type ReactElement } from 'react';
import { PROVENANCE_LABEL, rigCommand, type KitRow } from '../capture/kits';
import type { AudioUploadController } from '../upload/audioUpload';
import { EngineAudioUpload } from './EngineAudioUpload';

interface Props {
  rows: readonly KitRow[];
  sessionId: string;
  /** Read at click time and written only to the clipboard; it is never rendered. */
  ownerToken: string;
  capturing: boolean;
  /** Engine-audio upload controller; without it the card only reports status. */
  audio?: AudioUploadController;
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

export function CaptureKits({ rows, sessionId, ownerToken, capturing, audio }: Props): ReactElement {
  const [idState, setIdState] = useState<CopyState>('idle');
  const [commandState, setCommandState] = useState<CopyState>('idle');
  const [tokenState, setTokenState] = useState<CopyState>('idle');
  const command = rigCommand(sessionId);

  const copy = async (text: string, set: (state: CopyState) => void): Promise<void> => {
    set((await copyToClipboard(text)) ? 'copied' : 'failed');
  };
  const label = (state: CopyState, idle: string): string => (state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : idle);

  const phone = rows.find((row) => row.id === 'phone_video');
  const sources = rows.filter((row) => row.id !== 'phone_video');
  const chips = (row: KitRow): ReactElement => (
    <span className="kit-status">
      <span className={row.attached ? 'tag tag-ok' : 'tag tag-quiet'}>{row.attached ? 'Attached' : 'Waiting'}</span>
      {row.provenance && <span className={row.provenance === 'synthetic' ? 'tag tag-sim' : 'tag tag-quiet'}>{PROVENANCE_LABEL[row.provenance]}</span>}
      {row.notAssessed && <span className="tag tag-quiet">Not assessed</span>}
    </span>
  );

  return (
    <section aria-labelledby="kits-heading" className="kits" data-testid="capture-kits">
      <h3 id="kits-heading" className="sub-title">Pod, OBD and engine audio</h3>
      <p className="hint">These feed the Mechanical score. Anything left waiting shows as not checked.</p>
      <ul className="kit-list">
        {phone && (
          <li className="kit-row" data-testid={`kit-${phone.id}`}>
            <span className="kit-name">{phone.label}</span>
            {chips(phone)}
          </li>
        )}
        {sources.map((row) => (
          <li key={row.id} className="kit-row" data-testid={`kit-${row.id}`}>
            <span className="kit-name">{row.label}</span>
            {chips(row)}
            {row.id === 'rig_telemetry' && capturing && !row.attached && (
              <details className="kit-connect kit-wide">
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
                  <p className="hint" data-testid="rig-token-note">The command reads the owner token from the <code>UPCLOSE_OWNER_TOKEN</code> environment variable. Use Copy owner token and set it in your shell.</p>
                  <div>
                    <button type="button" className="btn btn-sm" onClick={() => { void copy(ownerToken, setTokenState); }}>{label(tokenState, 'Copy owner token')}</button>
                    <span role="status" className="sr-only">{tokenState === 'copied' ? 'Owner token copied to the clipboard' : tokenState === 'failed' ? 'Could not copy the owner token' : ''}</span>
                  </div>
                  <p className="hint" role="note">This key controls the inspection. Paste it only into your own rig.</p>
                </div>
              </details>
            )}
            {row.id === 'obd_snapshot' && !row.attached && capturing && <span className="kit-act muted">Sent by the pod command</span>}
            {row.id === 'engine_audio' && audio && <div className="kit-wide"><EngineAudioUpload upload={audio} attached={row.attached} capturing={capturing} /></div>}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function MissingKitsNotice({ rows }: { rows: readonly KitRow[] }): ReactElement | null {
  if (rows.length === 0) return null;
  return (
    <p className="notice notice-warn" role="status" data-testid="missing-kits">
      <span className="lead-in">Still waiting: </span>{rows.map((row) => row.label).join(', ')}. They will show as not checked in the report.
    </p>
  );
}
