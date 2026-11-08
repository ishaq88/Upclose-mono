import { cloneElement, useId, type ReactElement } from 'react';
import { cx } from './cx';

export interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  /** One input, textarea or select. Field gives it the id, the hint and error wiring. */
  children: ReactElement<{ id?: string; 'aria-describedby'?: string; 'aria-invalid'?: boolean }>;
  className?: string;
}

export function Field({ label, hint, error, children, className }: FieldProps): ReactElement {
  const auto = useId();
  const id = children.props.id ?? auto;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [children.props['aria-describedby'], hint ? hintId : null, error ? errorId : null].filter(Boolean).join(' ') || undefined;
  return (
    <div className={cx('field', error && 'field-invalid', className)}>
      <label htmlFor={id}>{label}</label>
      {cloneElement(children, { id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined })}
      {hint && <span id={hintId} className="hint">{hint}</span>}
      {error && <span id={errorId} className="field-error" role="alert">{error}</span>}
    </div>
  );
}
