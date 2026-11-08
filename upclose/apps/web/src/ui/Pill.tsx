import type { ReactElement, ReactNode } from 'react';
import { cx } from './cx';

export type PillTone = 'filmed' | 'ok' | 'not' | 'warn' | 'risk' | 'optional' | 'synthetic';

/** A small status label. The tone never replaces the words: always put the state in `children`. */
export function Pill({ tone, children, className }: { tone: PillTone; children: ReactNode; className?: string }): ReactElement {
  return <span className={cx('pill', `pill-${tone}`, className)}>{children}</span>;
}
