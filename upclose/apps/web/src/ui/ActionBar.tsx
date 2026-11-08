import type { ReactElement, ReactNode } from 'react';
import { cx } from './cx';

/** The page's actions: a sticky bottom bar (with the safe-area inset) up to 820 px, inline above that. Give the page bottom padding for it. */
export function ActionBar({ children, className, label }: { children: ReactNode; className?: string; label?: string }): ReactElement {
  return <div className={cx('action-bar', className)} role="group" aria-label={label}>{children}</div>;
}
