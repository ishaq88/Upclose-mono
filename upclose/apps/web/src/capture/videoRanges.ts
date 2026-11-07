import type { VideoRange } from '../api/client';

export interface RangeDraft { partNo: number; start: string; end: string; }

/** User declarations only. Neither validation nor selecting a zone verifies visual coverage. */
export function parseVideoRanges(drafts: RangeDraft[], durationSeconds: number | null): VideoRange[] {
  if (drafts.length > 8) throw new Error('Use at most eight marked sections.');
  const ranges = drafts.map((item) => {
    const start = Number(item.start), end = Number(item.end);
    if (item.start.trim() === '' || item.end.trim() === '' || !Number.isFinite(start) || !Number.isFinite(end)
      || start < 0 || end <= start || end > 180 || end - start > 60) {
      throw new Error('Each section needs a start and end in seconds, from 0 to 180, lasting no more than 60 seconds.');
    }
    if (durationSeconds !== null && end > durationSeconds) throw new Error('A marked section ends after this video ends.');
    if (!Number.isInteger(item.partNo) || item.partNo < 1 || item.partNo > 8) throw new Error('Choose a valid area.');
    return { part_no: item.partNo, start_ms: Math.round(start * 1000), end_ms: Math.round(end * 1000) };
  });
  if (new Set(ranges.map((item) => item.part_no)).size !== ranges.length) throw new Error('Mark each area only once.');
  const sorted = [...ranges].sort((a, b) => a.start_ms - b.start_ms);
  if (sorted.some((item, i) => item.end_ms <= item.start_ms || (i > 0 && sorted[i - 1]!.end_ms > item.start_ms))) {
    throw new Error('Marked sections must not overlap and must have a positive length.');
  }
  return ranges;
}
