import type { ReactElement, ReactNode } from 'react';
import type { UiError } from '../errors';
import { Notice } from '../ui';

/** Names the problem and the recovery. Uses an alert so assistive tech announces it once. */
export function ResultError({ error, children }: { error: UiError; children?: ReactNode }): ReactElement {
  return (
    <Notice tone="error" title={error.title} action={children ? <div className="res-actions">{children}</div> : undefined}>
      {error.message}
    </Notice>
  );
}
