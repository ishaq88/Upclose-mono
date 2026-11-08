import type { ReactElement, ReactNode } from 'react';
import { cx } from './cx';

export interface DisclosureProps {
  title: string;
  /** One line shown beside the title while collapsed. */
  summary?: string;
  defaultOpen?: boolean;
  /** Renders the title as an h2/h3/h4 so the page outline stays right. Omit for a plain label. */
  headingLevel?: 2 | 3 | 4;
  id?: string;
  className?: string;
  children: ReactNode;
}

/** A native `<details>`: keyboard and screen-reader behaviour come from the browser. */
export function Disclosure({ title, summary, defaultOpen = false, headingLevel, id, className, children }: DisclosureProps): ReactElement {
  const Heading = headingLevel ? (`h${headingLevel}` as 'h2' | 'h3' | 'h4') : 'span';
  return (
    <details className={cx('disc', className)} open={defaultOpen || undefined} id={id}>
      <summary>
        <Heading className="disc-title">{title}</Heading>
        {summary && <span className="disc-sum">{summary}</span>}
        <span className="disc-mark" aria-hidden="true" />
      </summary>
      <div className="disc-body">{children}</div>
    </details>
  );
}
