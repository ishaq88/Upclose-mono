import type { PaidReport } from '../report/model';
import report from './report.json';

/**
 * The public sample report. To publish a different one, replace report.json with a PaidReport
 * from GET /api/v1/reports/{slug} (owner, paid scope) and put one approved frame per finding and
 * candidate at public/sample/frames/<id>.jpg. HERO_POINTS only applies to the bundled van photo.
 */
export const SAMPLE_REPORT = report as unknown as PaidReport; // the bundled snapshot predates newer optional-in-practice fields

export function sampleFrameUrl(id: string): string {
  return `/sample/frames/${id}.jpg`;
}

/** Where the sample findings sit in the full hero photo (/assets/van.jpg, 1906 × 1228). */
export const HERO_PHOTO = { src: '/assets/van.jpg', width: 1906, height: 1228 } as const;
export const HERO_POINTS: Readonly<Record<string, { x: number; y: number }>> = {
  '3f6c1d2a-5b7e-4c1a-9d2e-1a2b3c4d5e01': { x: 1024, y: 822 },
  '3f6c1d2a-5b7e-4c1a-9d2e-1a2b3c4d5e02': { x: 393, y: 857 },
  '3f6c1d2a-5b7e-4c1a-9d2e-1a2b3c4d5e03': { x: 131, y: 786 },
};
