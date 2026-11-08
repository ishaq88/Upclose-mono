import type { ReactElement } from 'react';
import type { CategoryScores as Categories } from '../api/client';
import { formatIndex } from './model';

const NOT_CHECKED = 'Not checked';

function show(category: Categories['visual']): string {
  return category.state === 'assessed' && category.score !== null && Number.isFinite(category.score) ? formatIndex(category.score) : NOT_CHECKED;
}

/**
 * Visual, Mechanical and Overall. A category without evidence reads "Not checked" and never shows a number. When only the
 * visual inspection was done the API gives no overall number, and this shows "Visual only" with the server's note.
 */
export function CategoryScores({ categories }: { categories: Categories }): ReactElement {
  const { visual, mechanical, overall } = categories;
  const overallScored = overall.state === 'assessed' && overall.score !== null;
  const visualOnly = !overallScored && overall.basis === 'visual_only';
  return (
    <section aria-label="Category scores" className="category-scores" data-testid="category-scores">
      <dl className="category-list">
        <div className="category" data-testid="category-visual">
          <dt>Visual</dt>
          <dd className={show(visual) === NOT_CHECKED ? 'category-none' : 'category-score num'}>{show(visual)}</dd>
        </div>
        <div className="category" data-testid="category-mechanical">
          <dt>Mechanical</dt>
          <dd className={show(mechanical) === NOT_CHECKED ? 'category-none' : 'category-score num'}>{show(mechanical)}</dd>
        </div>
        <div className="category" data-testid="category-overall">
          <dt>Overall</dt>
          {overallScored
            ? <dd className="category-score num">{formatIndex(overall.score!)}</dd>
            : <dd className="category-none">{visualOnly ? 'Visual only' : NOT_CHECKED}</dd>}
        </div>
      </dl>
      {visualOnly && <p className="hint">{overall.note || 'Mechanical was not checked, so there is no overall score.'}</p>}
    </section>
  );
}
