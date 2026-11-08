import type { HTMLAttributes, ReactElement, ReactNode } from 'react';
import { cx } from './cx';

export type PillTone = 'filmed' | 'ok' | 'not' | 'warn' | 'risk' | 'optional' | 'synthetic';

/** A small status label. The tone never replaces the words: always put the state in `children`. */
export function Pill({ tone, children, className, ...rest }: { tone: PillTone; children: ReactNode } & Omit<HTMLAttributes<HTMLSpanElement>, 'children'>): ReactElement {
  return <span {...rest} className={cx('pill', `pill-${tone}`, className)}>{children}</span>;
}
