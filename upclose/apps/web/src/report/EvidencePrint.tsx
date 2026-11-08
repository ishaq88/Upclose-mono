import type { ReactElement, ReactNode } from 'react';
export { EvidenceFrame, SeverityPips } from '../ui/Evidence';

/** A finding card: frame on top (or beside on wide screens), details below. */
export function EvidencePrint({ frame, children, id, highlighted }: { frame: ReactNode; children: ReactNode; id?: string; highlighted?: boolean }): ReactElement {
  return (
    <article className={`ev-print${highlighted ? ' is-flashed' : ''}`} id={id}>
      {frame}
      <div className="ev-body">{children}</div>
    </article>
  );
}
