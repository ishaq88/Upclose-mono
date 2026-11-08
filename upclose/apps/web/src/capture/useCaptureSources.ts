import { useEffect, useState } from 'react';
import type { Api, CaptureSource } from '../api/client';
import { SOURCES_POLL_MS } from '../config';

/**
 * Attached capture-kit sources. Reads once on mount, then every `pollMs` while `polling` is true (capture is open);
 * polling stops as soon as it turns false (finalized or deleted). A failed read keeps the last known list.
 * Changing `readNow` forces an immediate read (for example right after an upload is accepted).
 */
export function useCaptureSources(api: Api, sessionId: string, polling: boolean, pollMs: number = SOURCES_POLL_MS, readNow: unknown = null): CaptureSource[] {
  const [sources, setSources] = useState<CaptureSource[]>([]);

  useEffect(() => {
    let cancelled = false;
    const read = (): void => {
      api.listSources(sessionId).then((next) => { if (!cancelled) setSources(next); }, () => { /* keep the last known list */ });
    };
    read();
    if (!polling) return () => { cancelled = true; };
    const timer = setInterval(read, pollMs);
    return () => { cancelled = true; clearInterval(timer); };
  }, [api, sessionId, polling, pollMs, readNow]);

  return sources;
}
