import { useId, type AnchorHTMLAttributes, type ButtonHTMLAttributes, type MouseEvent, type ReactElement, type ReactNode, type Ref } from 'react';
import { cx } from './cx';

export type ButtonVariant = 'primary' | 'secondary' | 'dark' | 'danger' | 'text';
export type ButtonSize = 'md' | 'sm';

interface CommonProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Full width. */
  block?: boolean;
  /** Leading icon (decorative). */
  icon?: ReactNode;
  /** Keeps the button focusable but inert, and says why to assistive tech. Use `disabled` when no reason is needed. */
  disabledReason?: string;
  className?: string;
  children: ReactNode;
}

export type ButtonProps = CommonProps & Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className' | 'children'> & { href?: undefined; ref?: Ref<HTMLButtonElement> };
export type LinkButtonProps = CommonProps & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'className' | 'children'> & { href: string };

/** The one button. Renders an `<a>` when `href` is given. Hover lifts 1px, press scales to 0.98. */
export function Button(props: ButtonProps | LinkButtonProps): ReactElement {
  const { variant = 'primary', size = 'md', block, icon, disabledReason, className, children, ...rest } = props;
  const reasonId = useId();
  const classes = cx('btn', `btn-${variant}`, size === 'sm' && 'btn-sm', block && 'btn-block', className);
  const content = (
    <>
      {icon && <span className="btn-icon" aria-hidden="true">{icon}</span>}
      <span>{children}</span>
      {disabledReason && <span id={reasonId} className="sr-only">{disabledReason}</span>}
    </>
  );
  if ('href' in rest && rest.href !== undefined) {
    return <a {...(rest as AnchorHTMLAttributes<HTMLAnchorElement>)} className={classes}>{content}</a>;
  }
  const { onClick, type = 'button', ...buttonRest } = rest as ButtonHTMLAttributes<HTMLButtonElement>;
  const blocked = disabledReason !== undefined;
  return (
    <button {...buttonRest} type={type} className={classes}
      aria-disabled={blocked ? true : undefined} aria-describedby={blocked ? reasonId : buttonRest['aria-describedby']}
      onClick={blocked ? (event: MouseEvent<HTMLButtonElement>) => event.preventDefault() : onClick}>
      {content}
    </button>
  );
}
