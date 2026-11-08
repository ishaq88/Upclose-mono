import type { ReactElement } from 'react';
import type { CategoryScores } from '../api/client';
import { formatIndex } from '../report/model';

export interface ScoreLineProps {
  categories: CategoryScores;
  /** Zones with a score, for the "needs 3 zones" reason. */
  zonesChecked: number;
}

function scoreOf(category: { score: number | null; state: string }): string | null {
  return category.state === 'assessed' && category.score !== null && Number.isFinite(category.score) ? formatIndex(category.score) : null;
}

/** Visual · Mechanical · Overall on one compact line. A missing score always says why, in a few words; one decimal, never rounded up. */
export function ScoreLine({ categories, zonesChecked }: ScoreLineProps): ReactElement {
  const { visual, mechanical, overall } = categories;
  const v = scoreOf(visual);
  const m = scoreOf(mechanical);
  const o = scoreOf(overall);
  const cells = [
    { id: 'visual', label: 'Visual', score: v, why: `needs 3 zones · ${zonesChecked} checked` },
    { id: 'mechanical', label: 'Mechanical', score: m, why: 'not recorded' },
    { id: 'overall', label: 'Overall', score: o, why: overall.basis === 'visual_only' ? 'visual only' : 'not available' },
  ];
  return (
    <dl className="score-line" aria-label="Category scores" data-testid="category-scores">
      {cells.map((cell) => (
        <div key={cell.id} className="score-cell" data-testid={`category-${cell.id}`}>
          <dt>{cell.label}</dt>
          <dd>{cell.score !== null ? <b className="num">{cell.score}</b> : <span className="muted">{cell.why}</span>}</dd>
        </div>
      ))}
    </dl>
  );
}
