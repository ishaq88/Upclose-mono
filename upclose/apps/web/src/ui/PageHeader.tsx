import { Fragment, type ReactElement, type ReactNode } from 'react';

export interface Crumb { label: string; href?: string }

export interface PageHeaderProps {
  crumbs?: ReadonlyArray<Crumb>;
  title: string;
  titleId?: string;
  meta?: ReactNode;
  actions?: ReactNode;
}

/** Breadcrumbs, the page's one h1, a meta line and an actions slot. */
export function PageHeader({ crumbs, title, titleId, meta, actions }: PageHeaderProps): ReactElement {
  return (
    <header className="page-header">
      <div className="page-header-main">
        {crumbs && crumbs.length > 0 && (
          <nav className="crumbs" aria-label="Breadcrumb">
            {crumbs.map((crumb, index) => (
              <Fragment key={crumb.label}>
                {index > 0 && <span aria-hidden="true">/</span>}
                {crumb.href ? <a href={crumb.href}>{crumb.label}</a> : <span aria-current="page">{crumb.label}</span>}
              </Fragment>
            ))}
          </nav>
        )}
        <h1 id={titleId} className="page-title">{title}</h1>
        {meta && <p className="page-meta">{meta}</p>}
      </div>
      {actions && <div className="page-header-actions">{actions}</div>}
    </header>
  );
}
