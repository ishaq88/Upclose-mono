import { useEffect, useRef, type ReactElement } from 'react';
import { mountGuide, type GuideHandle } from './mountGuide';

interface Props {
  partNo: number;
  onReady: () => void;
  /** Called once when WebGL or the model is unavailable; the caller shows the text steps instead. */
  onFail: () => void;
}

/** Lazy-loaded (it pulls in three.js). Mounts on first render, follows `partNo`, disposes on unmount. */
export default function Guide3D({ partNo, onReady, onFail }: Props): ReactElement {
  const host = useRef<HTMLDivElement | null>(null);
  const handle = useRef<GuideHandle | null>(null);
  const latest = useRef({ partNo, onReady, onFail });
  latest.current = { partNo, onReady, onFail };

  useEffect(() => {
    const el = host.current;
    if (!el) return undefined;
    let cancelled = false;
    mountGuide(el, latest.current.partNo).then((mounted) => {
      if (cancelled) { mounted.dispose(); return; }
      handle.current = mounted;
      mounted.setZone(latest.current.partNo);
      latest.current.onReady();
    }).catch(() => { if (!cancelled) latest.current.onFail(); });
    return () => { cancelled = true; handle.current?.dispose(); handle.current = null; };
  }, []);

  useEffect(() => { handle.current?.setZone(partNo); }, [partNo]);

  return <div ref={host} className="g3d" />;
}
