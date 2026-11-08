import type { ReactElement, ReactNode } from 'react';
import { cx } from './cx';

export interface BannerProps {
  tone?: 'info' | 'ok' | 'warn';
  children: ReactNode;
  /** The action, usually a text Button or a link. */
  action?: ReactNode;
}

/** One line of status with an optional action ("Driver side result is ready · View"). */
export function Banner({ tone = 'info', children, action }: BannerProps): ReactElement {
  return (
    <div className={cx('banner', tone !== 'info' && `banner-${tone}`)} role="status">
      <span>{children}</span>
      {action}
    </div>
  );
}
