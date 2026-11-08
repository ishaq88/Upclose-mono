import type { HTMLAttributes, ReactElement } from 'react';
import { cx } from './cx';

export type CardTone = 'default' | 'sunk' | 'danger-outline';

export interface CardProps extends HTMLAttributes<HTMLElement> {
  tone?: CardTone;
  padding?: 'md' | 'lg';
  /** Element to render; a div by default. */
  as?: 'div' | 'section' | 'article' | 'aside' | 'figure' | 'li';
}

export function Card({ tone = 'default', padding, as: Tag = 'div', className, ...rest }: CardProps): ReactElement {
  return <Tag {...rest} className={cx('card', tone !== 'default' && `card-${tone}`, padding && `card-pad-${padding}`, className)} />;
}
