import type { ReactElement, ReactNode } from 'react';
import type { UiError } from '../errors';

/** Names the problem and the recovery. Uses an alert so assistive tech announces it once. */
export function ResultError({ error, children }: { error: UiError; children?: ReactNode }): ReactElement {
  return (
    <div role="alert" className="notice notice-error res-error">
      <p className="res-error-title">{error.title}</p>
      <p>{error.message}</p>
      {children && <div className="res-actions">{children}</div>}
    </div>
  );
}
