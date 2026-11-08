import type { ReactElement } from 'react';

/**
 * Category scores for a report. PLACEHOLDER shape until the API ships the field: a category with no score is `null`,
 * which is never shown as a number. Overall with no Mechanical score is the Visual-only variant.
 */
export interface CategoryScoresView {
  visual: number | null;
  mechanical: number | null;
  overall: number | null;
}

const NOT_CHECKED = 'Not checked';

function show(score: number | null): string {
  return score === null || !Number.isFinite(score) ? NOT_CHECKED : String(Math.round(score));
}

export function CategoryScores({ scores }: { scores: CategoryScoresView }): ReactElement {
  const visualOnly = scores.visual !== null && scores.mechanical === null;
  const items: ReadonlyArray<{ id: string; label: string; value: number | null; note?: string }> = [
    { id: 'visual', label: 'Visual', value: scores.visual },
    { id: 'mechanical', label: 'Mechanical', value: scores.mechanical },
    { id: 'overall', label: 'Overall', value: scores.overall, note: visualOnly && scores.overall !== null ? 'Visual only' : undefined },
  ];
  return (
    <section aria-label="Category scores" className="category-scores" data-testid="category-scores">
      <dl className="category-list">
        {items.map((item) => (
          <div key={item.id} className="category" data-testid={`category-${item.id}`}>
            <dt>{item.label}</dt>
            <dd className={item.value === null ? 'category-none' : 'category-score num'}>
              {show(item.value)}
              {item.note && <span className="tag tag-quiet">{item.note}</span>}
            </dd>
          </div>
        ))}
      </dl>
      {visualOnly && <p className="hint">Mechanical checks were not done, so the overall score reflects the visual inspection only.</p>}
    </section>
  );
}
