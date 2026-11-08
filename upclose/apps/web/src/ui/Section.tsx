import { useId, type ReactElement, type ReactNode } from 'react';

export interface SectionProps {
  title: string;
  sub?: string;
  id?: string;
  children: ReactNode;
}

/** An h2 with an optional sub line, and consistent spacing between sections. */
export function Section({ title, sub, id, children }: SectionProps): ReactElement {
  const headingId = useId();
  return (
    <section className="section" id={id} aria-labelledby={headingId}>
      <div>
        <h2 id={headingId} className="section-title">{title}</h2>
        {sub && <p className="section-sub">{sub}</p>}
      </div>
      {children}
    </section>
  );
}
