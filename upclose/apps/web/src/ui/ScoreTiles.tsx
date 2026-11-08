import type { ReactElement } from 'react';
import type { CategoryScores } from '../api/client';
import { formatIndex } from '../report/model';

const NOT_CHECKED = 'Not checked';

type Category = { score: number | null; state: string };

function scoreOf(category: Category): string | null {
  return category.state === 'assessed' && category.score !== null && Number.isFinite(category.score) ? formatIndex(category.score) : null;
}

export interface ScoreTilesProps {
  categories: CategoryScores;
  /** The condition index out of 100. Shown only when Visual is not assessed: otherwise Visual is the condition index and repeating it would double-count. */
  conditionIndex?: number | null;
}

/**
 * Visual, Mechanical and Overall, each to one decimal and never rounded up. A category without evidence reads
 * "Not checked" and never a number; when only the visual inspection was done, Overall reads "Visual only".
 */
export function ScoreTiles({ categories, conditionIndex }: ScoreTilesProps): ReactElement {
  const { visual, mechanical, overall } = categories;
  const visualScore = scoreOf(visual);
  const mechanicalScore = scoreOf(mechanical);
  const overallScore = scoreOf(overall);
  const visualOnly = overallScore === null && overall.basis === 'visual_only';
  return (
    <section aria-label="Category scores" className="score-tiles" data-testid="category-scores">
      <dl className="category-list">
        <div className="category" data-testid="category-visual">
          <dt>Visual</dt>
          <dd className={visualScore === null ? 'category-none' : 'category-score num'}>{visualScore ?? NOT_CHECKED}</dd>
        </div>
        <div className="category" data-testid="category-mechanical">
          <dt>Mechanical</dt>
          <dd className={mechanicalScore === null ? 'category-none' : 'category-score num'}>{mechanicalScore ?? NOT_CHECKED}</dd>
        </div>
        <div className="category" data-testid="category-overall">
          <dt>Overall</dt>
          <dd className={overallScore === null ? 'category-none' : 'category-score num'}>{overallScore ?? (visualOnly ? 'Visual only' : NOT_CHECKED)}</dd>
        </div>
      </dl>
      {visualOnly && <p className="hint">{overall.note || 'Mechanical was not checked, so there is no overall score.'}</p>}
      {visualScore === null && conditionIndex !== undefined && conditionIndex !== null && Number.isFinite(conditionIndex) && (
        <p className="score-index">Condition index <b>{formatIndex(conditionIndex)}</b> out of 100</p>
      )}
    </section>
  );
}
