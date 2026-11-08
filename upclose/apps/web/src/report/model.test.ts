import { describe, expect, it } from 'vitest';
import { SAMPLE_REPORT } from '../sample/sample';
import { findingTitle, formatIndex, formatTime, isSerious, lowestZone, notChecked, orderFindings, scoredZones, verdict, type PaidFinding } from './model';

const base = SAMPLE_REPORT.findings[0]!;
const finding = (over: Partial<PaidFinding>): PaidFinding => ({ ...base, ...over });

describe('report model', () => {
  it('treats only non-internal flags as serious, like the server-rendered report', () => {
    expect(isSerious(finding({ flags: ['structural_rust_review'] }))).toBe(true);
    expect(isSerious(finding({ flags: ['duplicate_review', 'dedup_merged'] }))).toBe(false);
    expect(isSerious(finding({ flags: [] }))).toBe(false);
  });

  it('never scores a partly checked zone, so partial coverage cannot read as clean', () => {
    const zones = SAMPLE_REPORT.scores.zones;
    expect(scoredZones(zones).map((z) => z.zone)).not.toContain('tires');
    expect(notChecked(zones).map((z) => z.zone)).toContain('tires');
    expect(lowestZone(zones)?.zone).toBe('driver_side');
  });

  it('orders serious findings first and leads the verdict with them', () => {
    const ordered = orderFindings(SAMPLE_REPORT.findings);
    expect(isSerious(ordered[0]!)).toBe(true);
    expect(verdict(ordered).headline).toBe('1 serious issue');
    expect(verdict([]).headline).toBe('Nothing found');
    expect(verdict([], 5).headline).toBe('5 possible issues to check');
    expect(verdict([finding({ flags: [] })], 5).headline).toBe('1 minor finding');
    expect(verdict([finding({ flags: [] })]).headline).toBe('1 minor finding');
  });

  it('never rounds a score up and formats timecodes', () => {
    expect(formatIndex(99.96)).toBe('99.9');
    expect(formatIndex(87)).toBe('87.0');
    expect(formatTime(41000)).toBe('0:41');
    expect(formatTime(125000)).toBe('2:05');
    expect(formatTime(null)).toBeNull();
  });

  it('titles findings from a known component id, the sample ids, and falls back to the zone for unknown ids', () => {
    expect(findingTitle(finding({ concept: 'dent', component_id: 'hood' }))).toBe('Dent, hood');
    expect(findingTitle(finding({ concept: 'dent', component_id: 'passenger_sliding_door' as never }))).toBe('Dent, passenger sliding door');
    expect(findingTitle(finding({ concept: 'tire_wear', component_id: 'front_left_tire' as never }))).toBe('Tire wear, front left tire');
    const unknown = findingTitle(finding({ concept: 'scratch', zone: 'driver_side', component_id: 'not_a_real_part' as never }));
    expect(unknown).toBe('Scratch, driver side');
    for (const f of SAMPLE_REPORT.findings) expect(findingTitle(f)).not.toContain('undefined');
  });
});
