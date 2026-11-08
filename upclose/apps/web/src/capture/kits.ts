import type { CaptureSource, SourceKind, SourceProvenance } from '../api/client';

export type KitId = 'phone_video' | SourceKind;

export interface KitRow {
  id: KitId;
  label: string;
  attached: boolean;
  provenance: SourceProvenance | null;
  notAssessed: boolean;
}

const SOURCE_KITS: ReadonlyArray<{ id: SourceKind; label: string }> = [
  { id: 'rig_telemetry', label: 'Sensor pod' },
  { id: 'obd_snapshot', label: 'OBD' },
  { id: 'engine_audio', label: 'Engine audio' },
];

export const PROVENANCE_LABEL: Record<SourceProvenance, string> = {
  synthetic: 'Simulated',
  cached: 'Replayed',
  live: 'Live',
};

/** Rows for the Capture kits card. Phone video comes from the existing part/upload state, the rest from attached sources. */
export function kitRows(phoneVideoAttached: boolean, sources: readonly CaptureSource[]): KitRow[] {
  const rows: KitRow[] = [{ id: 'phone_video', label: 'Phone video', attached: phoneVideoAttached, provenance: null, notAssessed: false }];
  for (const kit of SOURCE_KITS) {
    const source = sources.find((item) => item.kind === kit.id);
    rows.push({
      id: kit.id, label: kit.label, attached: source !== undefined,
      provenance: source?.provenance ?? null,
      notAssessed: source?.summary.state === 'not_assessed',
    });
  }
  return rows;
}

export function missingKits(rows: readonly KitRow[]): KitRow[] {
  return rows.filter((row) => !row.attached);
}

export function rigCommand(sessionId: string): string {
  return `python -m upclose_rig demo-run --session ${sessionId} --api http://localhost:8000 --port COM3 --audio synth`;
}
