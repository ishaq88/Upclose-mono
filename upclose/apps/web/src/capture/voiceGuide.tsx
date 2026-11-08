import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from 'react';
import type { Api, VoiceGuideLine } from '../api/client';
import type { QualityHint } from './quality';

const PREF_KEY = 'upclose.voiceGuide.v1';

/** Whether the spoken guide is on. Defaults to on; every storage access is wrapped because storage can be blocked. */
export function loadVoicePreference(): boolean {
  try {
    return globalThis.localStorage?.getItem(PREF_KEY) !== 'off';
  } catch {
    return true;
  }
}

export function saveVoicePreference(enabled: boolean): void {
  try {
    globalThis.localStorage?.setItem(PREF_KEY, enabled ? 'on' : 'off');
  } catch {
    // Storage unavailable: the choice holds for this view only.
  }
}

/** Catalog line spoken at the start of a guided zone. */
export const zoneLineId = (zone: string): string => `zone_${zone}`;

/**
 * Catalog line for each client-side quality warning. The current catalog has no line for these (its warnings are
 * too dark, too shaky, too fast, car not in frame, which need frame analysis), so nothing is spoken for them yet.
 */
export const QUALITY_WARNING_LINE: Partial<Record<QualityHint['id'], string>> = {};

export type VoiceStatus = 'idle' | 'playing' | 'unavailable';

export interface VoiceGuide {
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
  /** Speak a catalog line. Call it from a tap. Never throws and never waits on the caller: failure leaves the caption only. */
  play: (lineId: string, fallbackCaption?: string) => void;
  stop: () => void;
  /** Server caption for a line, when the catalog loaded. Callers keep their own text as the fallback. */
  caption: (lineId: string) => string | null;
  /** The line last asked for, so a caption can always be shown. */
  current: { id: string; caption: string | null } | null;
  status: VoiceStatus;
}

const SILENT: VoiceGuide = {
  enabled: false, setEnabled: () => {}, play: () => {}, stop: () => {}, caption: () => null, current: null, status: 'idle',
};

const VoiceContext = createContext<VoiceGuide>(SILENT);

export function useVoiceGuide(): VoiceGuide {
  return useContext(VoiceContext);
}

/**
 * Spoken guidance from the fixed server catalog. Audio is optional: a failed catalog or audio fetch, a blocked
 * autoplay or an unsupported browser all end in captions only, and nothing here can block capture.
 */
export function VoiceGuideProvider({ api, children }: { api: Api; children: ReactNode }): ReactElement {
  const [enabled, setEnabledState] = useState<boolean>(loadVoicePreference);
  const [lines, setLines] = useState<VoiceGuideLine[]>([]);
  const [current, setCurrent] = useState<VoiceGuide['current']>(null);
  const [status, setStatus] = useState<VoiceStatus>('idle');
  const element = useRef<HTMLAudioElement | null>(null);
  const urls = useRef(new Map<string, string>());
  const requestId = useRef(0);
  const mounted = useRef(true);
  const linesRef = useRef(lines);
  linesRef.current = lines;

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    api.getVoiceGuide().then((catalog) => { if (!cancelled) setLines(catalog.lines); }, () => { /* captions fall back to local text */ });
    const cache = urls.current;
    return () => {
      mounted.current = false;
      cancelled = true;
      element.current?.pause();
      for (const url of cache.values()) URL.revokeObjectURL(url);
      cache.clear();
    };
  }, [api]);

  const caption = useCallback((id: string): string | null => linesRef.current.find((line) => line.id === id)?.caption ?? null, []);

  const stop = useCallback(() => {
    requestId.current += 1;
    element.current?.pause();
    setStatus('idle');
  }, []);

  const setEnabled = useCallback((next: boolean) => {
    setEnabledState(next);
    saveVoicePreference(next);
    if (!next) stop();
  }, [stop]);

  const play = useCallback((id: string, fallbackCaption?: string) => {
    if (!enabled) return;
    setCurrent({ id, caption: caption(id) ?? fallbackCaption ?? null });
    const mine = ++requestId.current;
    try {
      // Created inside the tap so the browser ties the element to the user's gesture.
      const audio = element.current ?? new Audio();
      element.current = audio;
      audio.pause();
      const cached = urls.current.get(id);
      const ready = cached
        ? Promise.resolve(cached)
        : api.getVoiceGuideAudio(id).then((blob) => {
          const url = URL.createObjectURL(blob);
          urls.current.set(id, url);
          return url;
        });
      ready.then(async (url) => {
        if (!mounted.current || mine !== requestId.current) return;
        audio.src = url;
        audio.onended = () => { if (mine === requestId.current) setStatus('idle'); };
        await audio.play();
        if (mounted.current && mine === requestId.current) setStatus('playing');
      }).catch(() => { if (mounted.current && mine === requestId.current) setStatus('unavailable'); });
    } catch {
      setStatus('unavailable');
    }
  }, [api, caption, enabled]);

  const value = useMemo<VoiceGuide>(
    () => ({ enabled, setEnabled, play, stop, caption, current, status }),
    [enabled, setEnabled, play, stop, caption, current, status],
  );
  return <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>;
}

/** The on/off switch and the caption of the line being spoken. The caption shows whether or not audio plays. */
export function VoiceGuideBar(): ReactElement {
  const voice = useVoiceGuide();
  return (
    <div className="voice-bar">
      <label className="voice-toggle">
        <input type="checkbox" checked={voice.enabled} onChange={(event) => voice.setEnabled(event.target.checked)} />
        <span>Voice guide</span>
      </label>
      {voice.enabled && voice.current && (
        <p className="voice-caption" role="status" data-testid="voice-caption">
          <span className="lead-in">{voice.status === 'unavailable' ? 'Voice unavailable, captions only: ' : 'Voice guide: '}</span>
          {voice.current.caption ?? ''}
        </p>
      )}
    </div>
  );
}
