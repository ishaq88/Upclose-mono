import { useCallback, useEffect, useRef, useState, type ReactElement } from 'react';

export interface LoupePoint {
  /** Position in the source photo, in its own pixels. */
  x: number;
  y: number;
  title: string;
  detail: string;
  time: string | null;
}

interface Props {
  src: string;
  width: number;
  height: number;
  alt: string;
  points: readonly LoupePoint[];
}

const ZOOM = 2.2;
const POS_Y = 0.6; // matches background-position-y on the stage

/** The hero photo with a magnifying loupe parked on each finding; on desktop the loupe follows the pointer. */
export function HeroLoupe({ src, width, height, alt, points }: Props): ReactElement {
  const stage = useRef<HTMLDivElement>(null);
  const loupe = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  const [free, setFree] = useState(false);

  const lens = useCallback((px: number, py: number, clamp: boolean) => {
    const st = stage.current, lp = loupe.current;
    if (!st || !lp) return;
    const W = st.clientWidth, H = st.clientHeight, s = Math.max(W / width, H / height);
    const ox = (W - width * s) / 2, oy = (H - height * s) * POS_Y, R = lp.offsetWidth / 2;
    if (clamp) { px = Math.min(Math.max(px, R + 8), W - R - 8); py = Math.min(Math.max(py, R + 8), H - R - 8); }
    lp.style.left = `${px}px`;
    lp.style.top = `${py}px`;
    lp.style.backgroundSize = `${width * s * ZOOM}px ${height * s * ZOOM}px`;
    lp.style.backgroundPosition = `${-((px - ox) * ZOOM - R)}px ${-((py - oy) * ZOOM - R)}px`;
    lp.dataset.side = px < W * 0.35 ? 'left' : px > W * 0.65 ? 'right' : 'center';
  }, [width, height]);

  const park = useCallback((i: number) => {
    const st = stage.current, p = points[i];
    if (!st || !p) return;
    const W = st.clientWidth, H = st.clientHeight, s = Math.max(W / width, H / height);
    lens((W - width * s) / 2 + p.x * s, (H - height * s) * POS_Y + p.y * s, true);
  }, [lens, points, width, height]);

  useEffect(() => {
    const st = stage.current;
    if (!st) return;
    const ro = new ResizeObserver(() => park(active));
    ro.observe(st);
    return () => ro.disconnect();
  }, [park, active]);

  const current = points[active];
  return (
    <figure className="hero-figure">
      <div ref={stage} className="stage" role="img" aria-label={alt} style={{ backgroundImage: `url(${src})` }}
        onPointerMove={(e) => {
          if (e.pointerType !== 'mouse') return;
          const r = e.currentTarget.getBoundingClientRect();
          setFree(true);
          lens(e.clientX - r.left, e.clientY - r.top, false);
        }}
        onPointerLeave={() => { setFree(false); park(active); }}>
        <div ref={loupe} className={free ? 'loupe is-free' : 'loupe'} style={{ backgroundImage: `url(${src})` }} aria-hidden="true">
          {current && <span className="loupe-tag">{current.title}{current.time && <b>{current.time}</b>}</span>}
        </div>
      </div>
      <figcaption className="legend">
        {points.map((p, i) => (
          <button key={p.title} type="button" aria-pressed={i === active} onClick={() => { setActive(i); park(i); }}>
            <span className="n">{i + 1}</span>
            <span><b>{p.title}</b><small>{p.detail}{p.time ? ` · ${p.time}` : ''}</small></span>
          </button>
        ))}
      </figcaption>
    </figure>
  );
}
