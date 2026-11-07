import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Api } from '../api/client';
import { ApiError } from '../api/client';
import { describeError, type UiError } from '../errors';
import { loadUploadRecords, saveUploadRecords } from '../session/storage';
import {
  abortableSleep, abortAttempt, isUploadAbort, resumeUpload, startUpload, UploadError,
  type UploadContentType, type UploadEnv, type UploadProgress, type UploadRecord, type UploadSource,
} from './multipart';

export type LocalPhase = 'preparing' | 'uploading' | 'completing' | 'failed';

/** Client-side view of one part's in-flight work. Acceptance is never inferred here: it comes from the server. */
export interface LocalUpload {
  phase: LocalPhase;
  progress?: UploadProgress;
  error?: UiError;
}

export interface UploadsApi {
  local: Readonly<Record<number, LocalUpload>>;
  records: Readonly<Record<number, UploadRecord>>;
  /** True while any piece transfer, creation or completion is in progress in this tab. */
  anyActive: boolean;
  stopTransfers: () => Promise<void>;
  start: (partNo: number, file: UploadSource, contentType: UploadContentType) => Promise<void>;
  /** Re-send what is missing, using the in-memory file or a reselected one. */
  resume: (partNo: number, file: UploadSource) => Promise<void>;
  /** Retake: cancel transfers, abort the server attempt, forget the record. Resolves true when the part is free. */
  retake: (partNo: number) => Promise<boolean>;
  /** The file held in memory for this part in this tab, if any (lost on reload). */
  getFile: (partNo: number) => UploadSource | undefined;
  dismissError: (partNo: number) => void;
}

const ACTIVE: ReadonlySet<LocalPhase> = new Set<LocalPhase>(['preparing', 'uploading', 'completing']);

function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

interface Options {
  api: Api;
  sessionId: string;
  /** Called after any server-visible change so state is re-read from the server. */
  onChanged: () => void;
  /** Called on 401 so the shell can show the recovery explanation. */
  onUnauthorized: () => void;
}

export function useUploads({ api, sessionId, onChanged, onUnauthorized }: Options): UploadsApi {
  const [local, setLocal] = useState<Record<number, LocalUpload>>({});
  const [records, setRecords] = useState<Record<number, UploadRecord>>(() => loadUploadRecords(sessionId));
  const recordsRef = useRef(records);
  const controllers = useRef(new Map<number, AbortController>());
  const running = useRef(new Map<number, Promise<void>>());
  const files = useRef(new Map<number, UploadSource>());

  const env: UploadEnv = useMemo(() => ({
    api,
    fetchImpl: (input, init) => globalThis.fetch(input, init),
    sleep: abortableSleep,
    now: () => Date.now(),
    random: () => Math.random(),
  }), [api]);

  const setRecord = useCallback((record: UploadRecord | null, partNo: number) => {
    const next = { ...recordsRef.current };
    if (record) next[partNo] = record;
    else delete next[partNo];
    recordsRef.current = next;
    setRecords(next);
    saveUploadRecords(sessionId, next);
  }, [sessionId]);

  const patch = useCallback((partNo: number, value: LocalUpload | null) => {
    setLocal((current) => {
      const next = { ...current };
      if (value) next[partNo] = value;
      else delete next[partNo];
      return next;
    });
  }, []);

  const fail = useCallback((partNo: number, error: unknown) => {
    if (error instanceof ApiError && (error.status === 401 || error.code === 'TOKEN_INVALID')) onUnauthorized();
    if (isUploadAbort(error)) {
      patch(partNo, null);
      return;
    }
    if (error instanceof UploadError && error.kind === 'gone') setRecord(null, partNo);
    // A rejected reselection must not be kept as "the file" for a later Try again.
    if (error instanceof UploadError && error.kind === 'wrong_file') files.current.delete(partNo);
    patch(partNo, { phase: 'failed', error: describeError(error) });
  }, [onUnauthorized, patch, setRecord]);

  const track = useCallback(async (partNo: number, run: (signal: AbortSignal) => Promise<unknown>): Promise<void> => {
    const controller = new AbortController();
    controllers.current.set(partNo, controller);
    const work = (async () => {
      try {
        await run(controller.signal);
        setRecord(null, partNo);
        files.current.delete(partNo);
        patch(partNo, null);
      } catch (error) {
        fail(partNo, error);
      } finally {
        controllers.current.delete(partNo);
        onChanged();
      }
    })();
    running.current.set(partNo, work);
    await work;
    running.current.delete(partNo);
  }, [fail, onChanged, patch, setRecord]);

  const onProgress = useCallback((partNo: number) => (progress: UploadProgress) => {
    patch(partNo, { phase: progress.phase, progress });
  }, [patch]);

  const start = useCallback(async (partNo: number, file: UploadSource, contentType: UploadContentType) => {
    files.current.set(partNo, file);
    patch(partNo, { phase: 'preparing' });
    const idempotencyKey = newIdempotencyKey();
    await track(partNo, (signal) => startUpload(env, {
      sessionId, partNo, file, contentType, idempotencyKey,
      onRecord: (record) => { setRecord(record, partNo); onChanged(); },
    }, { signal, onProgress: onProgress(partNo) }));
  }, [env, onChanged, onProgress, patch, sessionId, setRecord, track]);

  const resume = useCallback(async (partNo: number, file: UploadSource) => {
    const record = recordsRef.current[partNo];
    if (!record) return;
    files.current.set(partNo, file);
    patch(partNo, { phase: 'preparing' });
    await track(partNo, (signal) => resumeUpload(env, record, file, { signal, onProgress: onProgress(partNo) }));
  }, [env, onProgress, patch, track]);

  const retake = useCallback(async (partNo: number): Promise<boolean> => {
    controllers.current.get(partNo)?.abort();
    await running.current.get(partNo); // let the in-flight attempt unwind so its record is final
    const record = recordsRef.current[partNo];
    try {
      if (record) await abortAttempt(env, record.uploadId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      patch(partNo, { phase: 'failed', error: describeError(error) });
      onChanged();
      return false;
    }
    setRecord(null, partNo);
    files.current.delete(partNo);
    patch(partNo, null);
    onChanged();
    return true;
  }, [env, onChanged, onUnauthorized, patch, setRecord]);

  const anyActive = Object.values(local).some((entry) => ACTIVE.has(entry.phase));

  // Warn before the tab closes mid-upload: only what storage already holds survives, and an unsaved
  // in-browser recording cannot be recovered.
  useEffect(() => {
    if (!anyActive) return undefined;
    const handler = (event: BeforeUnloadEvent): void => { event.preventDefault(); };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [anyActive]);

  // Leaving the screen stops transfers; the server-side attempt stays open and can be resumed.
  useEffect(() => {
    const active = controllers.current;
    return () => { active.forEach((controller) => controller.abort()); };
  }, []);

  return {
    local, records, anyActive, start, resume, retake,
    stopTransfers: async () => {
      controllers.current.forEach((controller) => controller.abort());
      await Promise.allSettled([...running.current.values()]);
    },
    getFile: (partNo) => files.current.get(partNo),
    dismissError: (partNo) => patch(partNo, null),
  };
}
