import type { components } from '../generated/contracts';

type Schemas = components['schemas'];
export type PaidReport = Schemas['PaidReport'];
export type PaidFinding = Schemas['PaidFinding'];
export type CandidateFinding = Schemas['CandidateFinding'];
export type Zone = Schemas['Zone'];
export type ZoneScore = Schemas['ZoneScore'];
export type BoundingBox = Schemas['BoundingBox'];

/** Report order: scored zones first, the optional unscored zones last. */
export const ZONE_ORDER: readonly Zone[] = [
  'driver_side', 'passenger_side', 'front_rear_body', 'tires', 'engine_bay', 'underbody', 'dash_electrical', 'interior',
];

export const ZONE_LABEL: Record<Zone, string> = {
  driver_side: 'Driver side',
  passenger_side: 'Passenger side',
  front_rear_body: 'Front & rear, glass',
  tires: 'Tires',
  dash_electrical: 'Dashboard',
  engine_bay: 'Engine bay',
  underbody: 'Underbody',
  interior: 'Interior',
};

/** Zones the scoring config gives no required checks; the UI calls them optional. */
export const OPTIONAL_ZONES: ReadonlySet<Zone> = new Set(['dash_electrical', 'interior']);

const CONCEPT_NOUN: Record<PaidFinding['concept'], string> = {
  rust: 'Rust', paint_bubbling: 'Paint bubbling', dent: 'Dent', scratch: 'Scratch',
  glass_crack: 'Cracked glass', tire_wear: 'Tire wear', fluid_leak: 'Fluid leak',
};

const PLACE: Record<string, string> = {
  driver_rocker: 'driver-side sill', passenger_rocker: 'passenger-side sill', windshield: 'windshield',
  tire_fl: 'front left tire', tire_fr: 'front right tire', tire_rl: 'rear left tire', tire_rr: 'rear right tire',
  driver_front_door: 'driver front door', driver_rear_door: 'driver rear door',
  passenger_front_door: 'passenger front door', passenger_rear_door: 'passenger rear door',
  passenger_sliding_door: 'passenger sliding door', driver_sliding_door: 'driver sliding door',
  front_left_tire: 'front left tire', front_right_tire: 'front right tire', rear_left_tire: 'rear left tire', rear_right_tire: 'rear right tire',
  hood: 'hood', roof: 'roof', trunk: 'trunk', front_bumper: 'front bumper', rear_bumper: 'rear bumper',
};

/** Flags the backend uses for its own de-duplication; they never make a finding serious (mirrors domain/dedup.py). */
const INTERNAL_FLAGS: ReadonlySet<string> = new Set(['duplicate_review', 'dedup_merged']);

/** Same rule as the server-rendered report: any non-internal flag makes a finding high-risk. */
export function isSerious(finding: PaidFinding): boolean {
  return finding.flags.some((flag) => !INTERNAL_FLAGS.has(flag));
}

export function findingTitle(finding: PaidFinding | CandidateFinding): string {
  const named = 'component_id' in finding && finding.component_id ? PLACE[finding.component_id] : undefined;
  const place = named ?? ZONE_LABEL[finding.zone].toLowerCase();
  return `${CONCEPT_NOUN[finding.concept]}, ${place}`;
}

/** Serious first, then by severity, then by zone order; numbering follows this order everywhere. */
export function orderFindings(findings: readonly PaidFinding[]): PaidFinding[] {
  return [...findings].sort((a, b) =>
    Number(isSerious(b)) - Number(isSerious(a))
    || b.severity - a.severity
    || ZONE_ORDER.indexOf(a.zone) - ZONE_ORDER.indexOf(b.zone)
    || a.id.localeCompare(b.id));
}

export interface Verdict {
  serious: number;
  minor: number;
  /** One line a buyer can act on. */
  headline: string;
  advice: string;
}

/** `possible` counts unverified candidates: never call a report with candidates "Nothing found". */
export function verdict(findings: readonly PaidFinding[], possible = 0): Verdict {
  const serious = findings.filter(isSerious).length;
  const minor = findings.length - serious;
  const minorText = `${minor} minor ${minor === 1 ? 'finding' : 'findings'}`;
  if (serious > 0) {
    return {
      serious, minor,
      headline: `${serious} serious ${serious === 1 ? 'issue' : 'issues'}`,
      advice: `Have a mechanic check ${serious === 1 ? 'it' : 'them'} before a price is agreed.${minor ? ` The other ${minorText} ${minor === 1 ? 'is' : 'are'} lower risk.` : ''}`,
    };
  }
  if (minor > 0) return { serious, minor, headline: minorText.replace(/^./, (c) => c.toUpperCase()), advice: 'Nothing we found is flagged as serious. Check the zones we couldn’t see.' };
  if (possible > 0) {
    return {
      serious, minor,
      headline: `${possible} possible ${possible === 1 ? 'issue' : 'issues'} to check`,
      advice: `We spotted ${possible === 1 ? 'something' : 'spots'} we couldn’t confirm. Look at ${possible === 1 ? 'it' : 'them'} yourself or ask a mechanic before a price is agreed.`,
    };
  }
  return { serious, minor, headline: 'Nothing found', advice: 'Nothing visible in the zones we checked. Check the zones we couldn’t see.' };
}

export function sortedZones(zones: readonly ZoneScore[]): ZoneScore[] {
  return [...zones].sort((a, b) => ZONE_ORDER.indexOf(a.zone) - ZONE_ORDER.indexOf(b.zone));
}

/** Zones with a score: only fully assessed zones are scored, so a partial zone is never shown as clean. */
export function scoredZones(zones: readonly ZoneScore[]): ZoneScore[] {
  return sortedZones(zones).filter((zone) => zone.state === 'assessed' && zone.score !== null);
}

export function lowestZone(zones: readonly ZoneScore[]): ZoneScore | null {
  return scoredZones(zones).reduce<ZoneScore | null>((low, zone) => (low === null || (zone.score ?? 101) < (low.score ?? 101) ? zone : low), null);
}

export function notChecked(zones: readonly ZoneScore[]): ZoneScore[] {
  return sortedZones(zones).filter((zone) => zone.state !== 'assessed' || zone.score === null);
}

/** One decimal, truncated so a score is never rounded up. */
export function formatIndex(score: number): string {
  return (Math.floor(score * 10) / 10).toFixed(1);
}

export function formatTime(ms: number | null | undefined): string | null {
  if (ms === null || ms === undefined) return null;
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function formatDollars(cents: number): string {
  return `$${Math.round(cents / 100).toLocaleString('en-US')}`;
}

/** Evidence ring: centred on the bbox, sized to cover it, as percentages of a 4:3 frame. */
export function ringStyle(bbox: BoundingBox): { left: string; top: string; width: string } {
  const size = Math.min(70, Math.max(18, Math.max(bbox.w, bbox.h * 0.75) * 130));
  return { left: `${(bbox.x + bbox.w / 2) * 100}%`, top: `${(bbox.y + bbox.h / 2) * 100}%`, width: `${size}%` };
}
