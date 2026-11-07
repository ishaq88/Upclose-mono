import type { PartSummary, ProgressResponse } from '../api/client';
import { OPTIONAL_ZONES, ZONE_ORDER } from '../report/model';

export interface Stage {
  label: string;
  status: 'done' | 'now' | 'todo';
}

/**
 * Named stages derived only from what the progress data states.
 * Uploaded: some clip has reached the server. Car found: some clip finished processing (a clip that
 * cannot be read as a car fails instead). Checking damage: done once every zone is processed, failed or
 * skipped. Report: the server says the report is ready. Nothing is shown as current before any activity.
 */
export function stagesFor(progress: ProgressResponse | null): Stage[] {
  const parts = progress?.parts ?? [];
  const uploaded = parts.some((p) => p.state === 'uploaded' || p.state === 'processing' || p.state === 'processed');
  const carFound = parts.some((p) => p.state === 'processed');
  const settled = parts.length > 0 && parts.every((p) => p.state === 'processed' || p.state === 'failed' || p.state === 'skipped');
  const checked = settled && carFound;
  const reportReady = progress?.report?.state === 'ready';
  const done = [uploaded, carFound, checked, reportReady];
  const labels = ['Uploaded', 'Car found', 'Checking damage', 'Report'];
  const firstOpen = done.indexOf(false);
  return labels.map((label, i) => ({
    label,
    status: done[i] ? 'done' : i === firstOpen && uploaded ? 'now' : 'todo',
  }));
}

/** Core zones in report order, then the optional ones (dash_electrical, interior) last. */
export function orderedParts(parts: readonly PartSummary[]): PartSummary[] {
  const rank = (p: PartSummary): number => ZONE_ORDER.indexOf(p.zone) + (OPTIONAL_ZONES.has(p.zone) ? 100 : 0);
  return [...parts].sort((a, b) => rank(a) - rank(b));
}
