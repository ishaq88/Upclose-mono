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

/** The plain-text line that names every state present, so colour and hatch are never the only signal. */
export function coverageLine(zones: ReadonlyArray<ZoneCoverage>): string {
  const count = (state: ZoneCoverageState): number => zones.filter((zone) => zone.state === state).length;
  const parts = [`${count('assessed')} of ${zones.length} zones assessed`];
  if (count('partial') > 0) parts.push(`${count('partial')} only partly checked`);
  if (count('not_checked') > 0) parts.push(`${count('not_checked')} not checked`);
  if (count('optional') > 0) parts.push(`${count('optional')} optional`);
  return parts.join(' · ');
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
