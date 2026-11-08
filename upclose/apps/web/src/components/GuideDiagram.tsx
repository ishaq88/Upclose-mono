import type { ReactElement } from 'react';
import { guideFor } from '../capture/guide';
import { walkText } from '../capture/zones';

/**
 * Text-only stand-in for the 3D guide, shown when WebGL or the model is unavailable. The written steps beside it
 * carry the full instructions; this just keeps the viewer's place and its walk line.
 */
export function GuideDiagram({ partNo }: { partNo: number | null }): ReactElement {
  if (partNo === null) {
    return (
      <div className="g-fallback" data-testid="guide-fallback">
        <strong>Whole car</strong>
        <span>The 3D guide is not available on this device. The list shows which zones are filmed.</span>
      </div>
    );
  }
  const title = guideFor(partNo)?.title ?? 'Guide';
  return (
    <div className="g-fallback" data-testid="guide-fallback">
      <strong>{title}</strong>
      <span>The 3D guide is not available on this device. Follow the steps below.</span>
      <span className="g-fallback-walk">{walkText(partNo)}</span>
    </div>
  );
}
