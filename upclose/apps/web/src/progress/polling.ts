import type { ProgressResponse, SessionResponse } from '../api/client';
import { POLL_MAX_BACKOFF_MS, POLL_MAX_RETRY_AFTER_MS } from '../config';

export interface DelayInput {
  baseMs: number;
  /** Consecutive failed polls so far (0 after a success). */
  failures: number;
  /** Server Retry-After from the last failure, if any. */
  retryAfterSeconds: number | null;
  maxBackoffMs?: number;
}

/**
 * Delay before the next poll. Success polls at the base interval. After failures (429, 5xx, network)
 * the delay doubles per consecutive failure up to a ceiling, and a server Retry-After is never undercut.
 */
export function nextPollDelay({ baseMs, failures, retryAfterSeconds, maxBackoffMs = POLL_MAX_BACKOFF_MS }: DelayInput): number {
  if (failures <= 0) return baseMs;
  const backoff = Math.min(maxBackoffMs, baseMs * 2 ** Math.min(failures, 16));
  const server = retryAfterSeconds === null ? 0 : Math.min(POLL_MAX_RETRY_AFTER_MS, retryAfterSeconds * 1000);
  return Math.max(backoff, server);
}

/**
 * Ready reports keep polling because payment entitlements may be granted or revoked.
 * Deleting sessions and failed reports are terminal. Terminal means stop;
 * an explicit refresh() (a user action) polls again.
 */
export function isTerminal(session: Pick<SessionResponse, 'state'> | null, progress: Pick<ProgressResponse, 'state' | 'report'> | null): boolean {
  if (session && (session.state === 'deleting' || session.state === 'deleted')) return true;
  if (!progress) return false;
  if (progress.state === 'deleting' || progress.state === 'deleted') return true;
  return progress.state === 'finalized' && progress.report !== null && progress.report.state === 'failed';
}
