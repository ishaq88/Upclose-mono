/**
 * Owner session persistence. The bearer token lives in sessionStorage only (contract section 3): reload in
 * the same tab works, closing the tab may lose it, and nothing here ever reaches a URL or the DOM.
 * Every access is wrapped: storage can be blocked, full or empty (private windows).
 */
import type { UploadRecord } from '../upload/multipart';

const SESSION_KEY = 'upclose.session.v1';
const uploadsKey = (sessionId: string): string => `upclose.uploads.v1.${sessionId}`;

export interface StoredSession {
  sessionId: string;
  token: string;
}

function store(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

function read(key: string): unknown {
  try {
    const raw = store()?.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function write(key: string, value: unknown): void {
  try {
    store()?.setItem(key, JSON.stringify(value));
  } catch {
    // Storage unavailable: the page still works for this view, it just cannot survive a reload.
  }
}

function remove(key: string): void {
  try {
    store()?.removeItem(key);
  } catch {
    // ignore
  }
}

export function loadSession(): StoredSession | null {
  const value = read(SESSION_KEY) as Partial<StoredSession> | null;
  if (value && typeof value.sessionId === 'string' && typeof value.token === 'string') {
    return { sessionId: value.sessionId, token: value.token };
  }
  return null;
}

export function saveSession(session: StoredSession): void {
  write(SESSION_KEY, session);
}

export function clearSession(): void {
  const current = loadSession();
  remove(SESSION_KEY);
  if (current) { remove(uploadsKey(current.sessionId)); remove(`upclose.source.v1.${current.sessionId}`); }
}

function isRecord(value: unknown): value is UploadRecord {
  const v = value as Partial<UploadRecord> | null;
  return !!v && typeof v.uploadId === 'string' && typeof v.partId === 'string' && typeof v.partNo === 'number'
    && typeof v.size === 'number' && typeof v.chunkSize === 'number' && typeof v.chunkCount === 'number'
    && typeof v.fingerprint === 'string';
}

export function loadUploadRecords(sessionId: string): Record<number, UploadRecord> {
  const raw = read(uploadsKey(sessionId));
  const out: Record<number, UploadRecord> = {};
  if (raw && typeof raw === 'object') {
    for (const value of Object.values(raw)) if (isRecord(value)) out[value.partNo] = value;
  }
  return out;
}

export function saveUploadRecords(sessionId: string, records: Record<number, UploadRecord>): void {
  write(uploadsKey(sessionId), records);
}

/** Source resume metadata is deliberately separate from the eight guided-part records. */
export interface SourceResume { record: UploadRecord; }
export function loadSourceResume(sessionId: string): SourceResume | null {
  const value = read(`upclose.source.v1.${sessionId}`) as Partial<SourceResume> | null;
  return value && isRecord(value.record) ? { record: value.record } : null;
}
export function saveSourceResume(sessionId: string, value: SourceResume | null): void {
  if (value) write(`upclose.source.v1.${sessionId}`, value);
  else remove(`upclose.source.v1.${sessionId}`);
}
