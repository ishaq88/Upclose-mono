import { useState, type ReactElement, type ReactNode } from 'react';
import { cx } from './cx';

export interface DisclosureProps {
  title: string;
  /** One line shown beside the title while collapsed. */
  summary?: string;
  defaultOpen?: boolean;
  /** Renders the title as an h2/h3/h4 so the page outline stays right. Omit for a plain label. */
  headingLevel?: 2 | 3 | 4;
  id?: string;
  /** Visible words beside the plus/minus, for a closed and an open state (for example Show details / Hide details). */
  toggleLabel?: readonly [closed: string, open: string];
  /** Id of the title element, for an outer aria-labelledby. */
  titleId?: string;
  /** Test id for the one-line summary. */
  summaryTestId?: string;
  className?: string;
  children: ReactNode;
}

/** A native `<details>`: keyboard and screen-reader behaviour come from the browser. */
export function Disclosure({ title, summary, defaultOpen = false, headingLevel, id, titleId, summaryTestId, toggleLabel, className, children }: DisclosureProps): ReactElement {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const Heading = headingLevel ? (`h${headingLevel}` as 'h2' | 'h3' | 'h4') : 'span';
  return (
    <details className={cx('disc', toggleLabel && 'disc-labelled', className)} open={defaultOpen || undefined} id={id} onToggle={toggleLabel ? (event) => setIsOpen(event.currentTarget.open) : undefined}>
      <summary>
        <Heading id={titleId} className="disc-title">{title}</Heading>
        {summary && <span className="disc-sum" data-testid={summaryTestId}>{summary}</span>}
        {toggleLabel ? (
          <span className="disc-toggle"><span>{isOpen ? toggleLabel[1] : toggleLabel[0]}</span><span className="disc-mark" aria-hidden="true" /></span>
        ) : <span className="disc-mark" aria-hidden="true" />}
      </summary>
      <div className="disc-body">{children}</div>
    </details>
  );
}
