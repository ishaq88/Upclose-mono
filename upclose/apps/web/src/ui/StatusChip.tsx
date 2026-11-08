import type { ReactElement } from 'react';
import { cx } from './cx';

export interface StatusChipProps {
  filmed: number;
  total: number;
  /** Zones being checked right now; the dot pulses while this is above zero. */
  checking?: number;
}

/** "2 of 8 filmed · 1 checking". The pulse is static under reduced motion. */
export function StatusChip({ filmed, total, checking = 0 }: StatusChipProps): ReactElement {
  return (
    <span className={cx('status-chip', checking > 0 && 'status-chip-busy')} role="status">
      <span className="status-dot" aria-hidden="true" />
      {filmed} of {total} filmed{checking > 0 ? ` · ${checking} checking` : ''}
    </span>
  );
}
