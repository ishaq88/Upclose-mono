import { useState, type FormEvent, type ReactElement } from 'react';
import type { Api } from '../api/client';
import { describeError } from '../errors';
import { Button, Notice } from '../ui';

const MAX_LABEL_CHARS = 80;

interface Props {
  api: Api;
  sessionId: string;
  label: string | null;
  /** Called with the name the server stored. */
  onRenamed: (label: string | null) => void;
}

/** Inline rename for an inspection, shown next to its title. Owner only; the server cleans and stores the name. */
export function RenameLabel({ api, sessionId, label, onRenamed }: Props): ReactElement {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(label ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const updated = await api.updateLabel(sessionId, draft.trim());
      onRenamed(updated.vehicle_label ?? null);
      setEditing(false);
    } catch (caught) {
      setError(describeError(caught).message);
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return <Button variant="secondary" size="sm" onClick={() => { setDraft(label ?? ''); setError(null); setEditing(true); }}>Rename</Button>;
  }
  return (
    <form className="rename" onSubmit={(event) => { void save(event); }}>
      <label>
        <span className="sr-only">Inspection name</span>
        <input type="text" value={draft} maxLength={MAX_LABEL_CHARS} placeholder="For example, 2014 Honda Civic" onChange={(event) => setDraft(event.target.value)} />
      </label>
      <Button type="submit" size="sm" disabled={busy || draft.trim() === ''}>Save name</Button>
      <Button variant="secondary" size="sm" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
      {error && <Notice tone="error">{error}</Notice>}
    </form>
  );
}
