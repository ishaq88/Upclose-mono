import type { ReactElement } from 'react';
import type { CategoryScores } from '../api/client';
import { formatIndex } from '../report/model';

export interface ScoreBandProps {
  /** What the API sent; absent on older reports. */
  categories: CategoryScores | null | undefined;
  /** Older reports: the headline condition index stands in for Visual. */
  headline: number | null;
  /** Zones with a score, for the "needs 3 zones" reason. */
  zonesChecked: number;
}

function numberOf(score: number | null | undefined, state: string): string | null {
  return state === 'assessed' && score !== null && score !== undefined && Number.isFinite(score) ? formatIndex(score) : null;
}

/**
 * Visual, Mechanical and Overall as three equal tiles at the top of a report. One decimal, never rounded up. A missing
 * score reads "Not checked" with a few words of reason; a server note is shown under the tiles as sent.
 */
export function ScoreBand({ categories, headline, zonesChecked }: ScoreBandProps): ReactElement {
  const visual = categories ? numberOf(categories.visual.score, categories.visual.state) : numberOf(headline, 'assessed');
  const mechanical = categories ? numberOf(categories.mechanical.score, categories.mechanical.state) : null;
  const overall = categories ? numberOf(categories.overall.score, categories.overall.state) : null;
  const visualOnly = overall === null && (categories ? categories.overall.basis === 'visual_only' : visual !== null);
  const tiles = [
    { id: 'visual', label: 'Visual', score: visual, why: `needs 3 zones · ${zonesChecked} checked` },
    { id: 'mechanical', label: 'Mechanical', score: mechanical, why: 'not recorded' },
    { id: 'overall', label: 'Overall', score: overall, why: visualOnly ? 'visual only' : 'not available' },
  ];
  const note = categories?.overall.note;
  return (
    <section aria-label="Category scores" className="score-band" data-testid="category-scores">
      <dl className="band-list">
        {tiles.map((tile) => (
          <div key={tile.id} className={`band-tile${tile.id === 'overall' ? ' band-dark' : ''}`} data-testid={`category-${tile.id}`}>
            <dt>{tile.label}</dt>
            <dd>
              {tile.score !== null
                ? <span className="band-num num">{tile.score}</span>
                : <><span className="band-none">Not checked</span><span className="band-why">{tile.why}</span></>}
            </dd>
          </div>
        ))}
      </dl>
      {note && <p className="hint">{note}</p>}
    </section>
  );
}
