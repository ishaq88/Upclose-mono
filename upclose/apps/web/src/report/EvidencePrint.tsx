import { useEffect, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { formatTime, ringStyle, type BoundingBox } from './model';

interface FrameProps {
  /** Ready-to-use image address (sample, data URL). Omit it when `resolve` supplies the image. */
  src?: string;
  /** Loads the image only once the frame is near the viewport (owner frames: token in a header, never in a URL). */
  resolve?: () => Promise<string>;
  bbox: BoundingBox;
  timestampMs: number | null | undefined;
  /** What the frame shows; used for alt text and the enlarged caption. */
  label: string;
  className?: string;
}

function useLazyFrame(resolve: (() => Promise<string>) | undefined): { url: string | null; failed: boolean; ref: (node: HTMLElement | null) => void } {
  const [node, setNode] = useState<HTMLElement | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [near, setNear] = useState(false);
  const latest = useRef(resolve);
  latest.current = resolve;
  const wanted = resolve !== undefined;
  useEffect(() => {
    if (!wanted || near) return;
    if (!node || typeof IntersectionObserver === 'undefined') { setNear(true); return; }
    const observer = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) setNear(true); }, { rootMargin: '300px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [wanted, near, node]);
  useEffect(() => {
    if (!wanted || !near) return;
    let live = true;
    latest.current?.().then((value) => { if (live) setUrl(value); }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [wanted, near]);
  return { url, failed, ref: setNode };
}

/** An evidence frame with its orange ring and timecode. Tap or click to see it larger. */
export function EvidenceFrame({ src, resolve, bbox, timestampMs, label, className = '' }: FrameProps): ReactElement {
  const dialog = useRef<HTMLDialogElement>(null);
  const time = formatTime(timestampMs);
  const lazy = useLazyFrame(resolve);
  const url = resolve ? lazy.url : src;
  if (resolve && lazy.failed) return <div ref={lazy.ref} className={`ev-frame ev-missing ${className}`} role="status">Image could not be loaded</div>;
  const art = (
    <>
      {url ? <img src={url} alt="" loading="lazy" decoding="async" /> : null}
      <span className="ev-ring" style={ringStyle(bbox)} aria-hidden="true" />
      {time && <span className="ev-stamp">{time}</span>}
    </>
  );
  return (
    <>
      <button type="button" ref={lazy.ref} className={`ev-frame ${className}`} onClick={() => dialog.current?.showModal()}
        aria-label={`Enlarge frame${time ? ` at ${time}` : ''}: ${label}`}>
        {art}
      </button>
      <dialog ref={dialog} className="ev-dialog" aria-label={label} onClick={(e) => { if (e.target === dialog.current) dialog.current?.close(); }}>
        <div className="ev-frame ev-frame-large">{art}</div>
        <div className="ev-dialog-bar">
          <b>{label}{time ? ` · ${time}` : ''}</b>
          <form method="dialog"><button className="btn btn-sm btn-dark">Close</button></form>
        </div>
      </dialog>
    </>
  );
}

/** A finding card: frame on top (or beside on wide screens), details below. */
export function EvidencePrint({ frame, children, id, highlighted }: { frame: ReactNode; children: ReactNode; id?: string; highlighted?: boolean }): ReactElement {
  return (
    <article className={`ev-print${highlighted ? ' is-flashed' : ''}`} id={id}>
      {frame}
      <div className="ev-body">{children}</div>
    </article>
  );
}

export function SeverityPips({ severity }: { severity: number }): ReactElement {
  return (
    <span className="ev-pips" aria-hidden="true">
      {[1, 2, 3, 4, 5].map((n) => <i key={n} className={n <= severity ? 'on' : undefined} />)}
    </span>
  );
}
