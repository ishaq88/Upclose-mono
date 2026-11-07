import { useEffect, useRef, useState, type ReactElement } from 'react';
import { COMPONENT_POSITION, mountMap, type MapInput, type MapPin, type SideState } from './carMap';
import { findingTitle, isSerious, type PaidFinding, type ZoneScore } from './model';

interface Props {
  findings: readonly PaidFinding[];
  zones: readonly ZoneScore[];
  onPin: (id: string) => void;
}

const SIDE_FALLBACK: Partial<Record<PaidFinding['zone'], [number, number]>> = {
  driver_side: [1, 0], passenger_side: [-1, 0], front_rear_body: [0, 1.1], tires: [1, 0.6],
};

function sideState(zones: readonly ZoneScore[], zone: ZoneScore['zone']): SideState {
  const row = zones.find((z) => z.zone === zone);
  return row?.state ?? 'not_assessed';
}

/** Lazy-loaded wrapper: three.js and the van model load only when the map is on screen. */
export default function TopDownMap({ findings, zones, onPin }: Props): ReactElement {
  const host = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let dispose: (() => void) | undefined;
    let alive = true;
    const pins: MapPin[] = findings.map((f, i) => {
      const [u, v] = (f.component_id && COMPONENT_POSITION[f.component_id]) || SIDE_FALLBACK[f.zone] || [0, 0];
      return { n: i + 1, id: `finding-${i + 1}`, label: findingTitle(f), serious: isSerious(f), u, v };
    });
    const front = sideState(zones, 'front_rear_body');
    const input: MapInput = {
      sides: { driver: sideState(zones, 'driver_side'), passenger: sideState(zones, 'passenger_side'), front, rear: front },
      pins, onPin,
    };
    mountMap(el, input)
      .then((d) => { if (alive) { dispose = d; setStatus('ready'); } else d(); })
      .catch(() => { if (alive) setStatus('failed'); });
    return () => { alive = false; dispose?.(); };
  }, [findings, zones, onPin]);
  return (
    <div className="map" ref={host}>
      <span className="map-front" aria-hidden="true">Front</span>
      {status === 'loading' && <span className="map-status">Loading map…</span>}
      {status === 'failed' && <span className="map-status">The map couldn't load. The findings are listed beside it.</span>}
    </div>
  );
}
