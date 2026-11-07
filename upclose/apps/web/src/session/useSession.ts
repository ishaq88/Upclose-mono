import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, type Api, type ProgressResponse, type SessionResponse } from '../api/client';
import { SESSION_POLL_MS } from '../config';
import { describeError, type UiError } from '../errors';
import { isTerminal, nextPollDelay } from '../progress/polling';

export interface SessionData {
  session: SessionResponse | null;
  /** Owner progress (part states, free Part-1 verdict, report state) from the same poll; null until first read. */
  progress: ProgressResponse | null;
  /** The last read error, cleared by the next fully successful poll. */
  error: UiError | null;
  /** True once the server has said the token is not valid. Polling stops. */
  unauthorized: boolean;
  refresh: () => void;
  markUnauthorized: () => void;
}

function isAuthFailure(caught: unknown): boolean {
  return caught instanceof ApiError && (caught.status === 401 || caught.status === 404 || caught.code === 'TOKEN_INVALID');
}

/**
 * Server-owned session state and the single poll source of truth. Each tick reads GET /sessions/{id} and
 * GET /sessions/{id}/progress together (coalesced: one in flight, at most one queued). Part and report states
 * shown in the UI come only from here; the client never invents an "uploaded", "processed" or "done" state.
 *
 * Failures (429, 5xx, network) back off exponentially and honour Retry-After; a user-triggered refresh()
 * cannot hammer through an active back-off. Ready reports still poll for entitlement changes.
 */
export function useSession(api: Api, sessionId: string, pollMs: number = SESSION_POLL_MS): SessionData {
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [progress, setProgress] = useState<ProgressResponse | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const [unauthorized, setUnauthorized] = useState(false);
  const stopped = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(false);
  const again = useRef(false);
  const failures = useRef(0);
  const notBefore = useRef(0);

  const load = useCallback(async (): Promise<void> => {
    if (stopped.current) return;
    if (inFlight.current) {
      again.current = true; // coalesce: one more read after the current one
      return;
    }
    inFlight.current = true;
    let latestSession: SessionResponse | null = null;
    let latestProgress: ProgressResponse | null = null;
    let failure: unknown = null;
    let retryAfter: number | null = null;
    try {
      const [sessionResult, progressResult] = await Promise.allSettled([api.getSession(sessionId), api.getProgress(sessionId)]);
      if (stopped.current) return;
      if (sessionResult.status === 'fulfilled') {
        latestSession = sessionResult.value;
        setSession(sessionResult.value);
      } else failure = sessionResult.reason;
      if (progressResult.status === 'fulfilled') {
        latestProgress = progressResult.value;
        setProgress(progressResult.value);
      } else failure ??= progressResult.reason;
      for (const result of [sessionResult, progressResult]) {
        if (result.status === 'rejected' && isAuthFailure(result.reason)) failure = result.reason;
        if (result.status === 'rejected' && result.reason instanceof ApiError && result.reason.retryAfterSeconds !== null) {
          retryAfter = Math.max(retryAfter ?? 0, result.reason.retryAfterSeconds);
        }
      }
      if (failure === null) {
        setError(null);
      } else {
        if (isAuthFailure(failure)) {
          stopped.current = true;
          setUnauthorized(true);
          setSession(null);
          setProgress(null);
        }
        setError(describeError(failure));
      }
    } finally {
      inFlight.current = false;
    }
    if (stopped.current) return;
    failures.current = failure === null ? 0 : failures.current + 1;
    const delay = nextPollDelay({ baseMs: pollMs, failures: failures.current, retryAfterSeconds: retryAfter });
    notBefore.current = failures.current > 0 ? Date.now() + delay : 0;
    if (again.current && failures.current === 0) {
      again.current = false;
      void load();
      return;
    }
    again.current = false;
    if (failure === null && isTerminal(latestSession, latestProgress)) return; // nothing more to learn
    timer.current = setTimeout(() => { void load(); }, delay);
  }, [api, pollMs, sessionId]);

  useEffect(() => {
    stopped.current = false;
    void load();
    return () => {
      stopped.current = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  const refresh = useCallback(() => {
    if (stopped.current) return;
    if (timer.current) clearTimeout(timer.current);
    const wait = notBefore.current - Date.now();
    if (wait > 0) {
      timer.current = setTimeout(() => { void load(); }, wait); // still backing off: do not hammer
      return;
    }
    void load();
  }, [load]);

  const markUnauthorized = useCallback(() => {
    stopped.current = true;
    setUnauthorized(true);
    setSession(null);
    setProgress(null);
  }, []);

  return { session, progress, error, unauthorized, refresh, markUnauthorized };
}
