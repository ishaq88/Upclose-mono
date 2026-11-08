import type { ReactElement, ReactNode } from 'react';
import { cx } from './cx';

export type NoticeTone = 'info' | 'ok' | 'warn' | 'error';

export interface NoticeProps {
  tone?: NoticeTone;
  title?: string;
  children?: ReactNode;
  /** A button or link, shown under the text. */
  action?: ReactNode;
  className?: string;
  /** Overrides the default: errors are alerts, the rest have no role. */
  role?: 'status' | 'alert';
}

/** Errors are announced (role=alert); info and warn are plain content. */
export function Notice({ tone = 'info', title, children, action, className, role }: NoticeProps): ReactElement {
  return (
    <div className={cx('notice', `notice-${tone}`, className)} role={role ?? (tone === 'error' ? 'alert' : undefined)}>
      {title && <p className="notice-title">{title}</p>}
      {children !== undefined && children !== null && (typeof children === 'string' ? <p>{children}</p> : children)}
      {action && <div className="notice-action">{action}</div>}
    </div>
  );
}
