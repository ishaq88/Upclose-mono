import type { ReactElement } from 'react';

export type ZoneCoverageState = 'assessed' | 'partial' | 'not_checked' | 'optional';

export interface ZoneCoverage {
  label: string;
  state: ZoneCoverageState;
}

const STATE_WORD: Record<ZoneCoverageState, string> = {
  assessed: 'assessed',
  partial: 'only partly checked',
  not_checked: 'not checked',
  optional: 'optional, not filmed',
};

const lower = (label: string): string => label.charAt(0).toLowerCase() + label.slice(1);

/**
 * One statement, one denominator (the required zones, optional ones excluded), naming the zones, so colour and hatch are never the
 * only signal. Example: "In the index: driver side, passenger side (2 of 6). Partly checked: tires. Not checked: engine bay. Optional: dashboard."
 */
export function coverageLine(zones: ReadonlyArray<ZoneCoverage>): string {
  const names = (state: ZoneCoverageState): string => zones.filter((zone) => zone.state === state).map((zone) => lower(zone.label)).join(', ');
  const has = (state: ZoneCoverageState): boolean => zones.some((zone) => zone.state === state);
  const required = zones.filter((zone) => zone.state !== 'optional').length;
  const inIndex = zones.filter((zone) => zone.state === 'assessed').length;
  const parts = [`In the index: ${has('assessed') ? names('assessed') : 'no zones'} (${inIndex} of ${required})`];
  if (has('partial')) parts.push(`Partly checked: ${names('partial')}`);
  if (has('not_checked')) parts.push(`Not checked: ${names('not_checked')}`);
  if (has('optional')) parts.push(`Optional: ${names('optional')}`);
  return `${parts.join('. ')}.`;
}

/** One segment per zone: solid assessed, half partial, hatched not checked, grey optional. Unknown is never drawn as clean. */
export function CoverageBar({ zones }: { zones: ReadonlyArray<ZoneCoverage> }): ReactElement {
  return (
    <div className="coverage" data-testid="coverage-bar">
      <div className="coverage-bar" aria-hidden="true">
        {zones.map((zone) => <span key={zone.label} className={`coverage-seg coverage-${zone.state}`} title={`${zone.label}: ${STATE_WORD[zone.state]}`} />)}
      </div>
      <p className="coverage-line">{coverageLine(zones)}</p>
    </div>
  );
}
