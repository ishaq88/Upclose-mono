import { lazy, Suspense, useCallback, useState, type ReactElement } from 'react';
import { GuideDiagram } from '../components/GuideDiagram';
import type { GuideStep } from './guide';
import { walkText } from './zones';
import './capture.css';

// three.js ships as its own chunk, fetched only when a capture screen shows a guide.
const Guide3D = lazy(() => import('./Guide3D'));

/** Accessible name for the 3D scene. The underbody one repeats the safety rule: nobody goes under a car. */
function sceneLabel(guide: GuideStep): string {
  if (guide.partNo === 7) {
    return 'Underbody, low sweep: a 3D guide showing the path along the side of the car. You stay standing and never go under the car.';
  }
  return `${guide.title}: 3D guide showing the walking path around the car.`;
}

/** The 3D viewer with its walk-instruction chip, or the text fallback when 3D cannot run. */
export function GuideViewer({ guide }: { guide: GuideStep }): ReactElement {
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const onReady = useCallback(() => setStatus('ready'), []);
  const onFail = useCallback(() => setStatus('failed'), []);

  if (status === 'failed') {
    return <div className="viewer viewer-text"><GuideDiagram partNo={guide.partNo} /></div>;
  }
  return (
    <div className="viewer">
      <div className="viewer-scene" role="img" aria-label={sceneLabel(guide)}>
        <Suspense fallback={null}>
          <Guide3D partNo={guide.partNo} onReady={onReady} onFail={onFail} />
        </Suspense>
      </div>
      {status === 'loading' && <div className="v-load" role="status">Loading 3D guide…</div>}
      <p className="v-chip"><i aria-hidden="true" />{walkText(guide.partNo)}</p>
      <span className="v-hint" aria-hidden="true">Drag to turn the car</span>
    </div>
  );
}
