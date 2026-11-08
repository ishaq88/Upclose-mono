import type { ReactElement } from 'react';
import { cx } from './cx';

export interface ChoiceOption<V extends string> {
  value: V;
  title: string;
  description: string;
  tag?: string;
  /** `quiet` for a grey tag, otherwise the accent tag. */
  tagTone?: 'accent' | 'quiet';
}

export interface ChoiceTilesProps<V extends string> {
  name: string;
  /** Read by assistive tech; the tiles themselves carry the visible text. */
  legend: string;
  value: V;
  onChange: (value: V) => void;
  options: ReadonlyArray<ChoiceOption<V>>;
}

/** Radio tiles with a tag, a title and a description (the start screen's mode choice). */
export function ChoiceTiles<V extends string>({ name, legend, value, onChange, options }: ChoiceTilesProps<V>): ReactElement {
  return (
    <fieldset className="tiles">
      <legend className="sr-only">{legend}</legend>
      {options.map((option) => (
        <label key={option.value} className="tile">
          <input type="radio" name={name} value={option.value} checked={value === option.value} onChange={() => onChange(option.value)} />
          <span className="tile-top">
            {option.tag ? <span className={cx('tag', option.tagTone === 'quiet' && 'tag-quiet')}>{option.tag}</span> : <span />}
            <span className="radio" aria-hidden="true" />
          </span>
          <strong>{option.title}</strong>
          <span className="muted">{option.description}</span>
        </label>
      ))}
    </fieldset>
  );
}
