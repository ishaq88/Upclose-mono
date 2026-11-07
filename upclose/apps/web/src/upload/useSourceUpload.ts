import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, type Api, type VideoRange, type VideoUploadResponse } from '../api/client';
import { describeError, type UiError } from '../errors';
import { loadSourceResume, saveSourceResume } from '../session/storage';
import { fingerprintBlob, isUploadAbort, resumeUpload, startUpload, type UploadContentType, type UploadProgress, type UploadRecord } from './multipart';
import { sourceUploadEnv } from './sourceAdapter';

interface Options { api: Api; sessionId: string; enabled: boolean; onChanged: () => void; onUnauthorized: () => void; }

export function useSourceUpload({ api, sessionId, enabled, onChanged, onUnauthorized }: Options) {
  const [source, setSource] = useState<VideoUploadResponse | null | undefined>(undefined);
  const [record, setRecordState] = useState<UploadRecord | null>(() => loadSourceResume(sessionId)?.record ?? null);
  const recordRef = useRef(record);
  const [error, setError] = useState<UiError | null>(null);
  const [pollError, setPollError] = useState<UiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const controller = useRef<AbortController | null>(null);
  const work = useRef<Promise<void> | null>(null);
  const intent = useRef<{ signature: string; key: string } | null>(null);
  const mounted = useRef(true);
  const readSequence = useRef(0);
  const pollDelay = useRef(2000);

  const setRecord = useCallback((value: UploadRecord | null) => {
    recordRef.current = value;
    saveSourceResume(sessionId, value ? { record: value } : null);
    if (mounted.current) setRecordState(value);
  }, [sessionId]);

  const fail = useCallback((caught: unknown) => {
    const described = describeError(caught);
    if (described.action === 'new_session') onUnauthorized();
    if (mounted.current && !isUploadAbort(caught)) setError(described);
  }, [onUnauthorized]);

  const refresh = useCallback(async () => {
    const sequence = ++readSequence.current;
    try {
      const response = await api.getVideoSource(sessionId);
      if (sequence !== readSequence.current) return;
      pollDelay.current = 2000;
      if (mounted.current) { setSource(response.source ?? null); setPollError(null); }
      if (response.source?.state === 'accepted' || response.source?.state === 'aborted') setRecord(null);
    } catch (caught) {
      if (sequence !== readSequence.current) return;
      const described = describeError(caught);
      if (described.action === 'new_session') onUnauthorized();
      if (mounted.current) setPollError(described);
      pollDelay.current = Math.max(Math.min(60000, pollDelay.current * 2), caught instanceof ApiError ? (caught.retryAfterSeconds ?? 0) * 1000 : 0);
    }
  }, [api, sessionId, onUnauthorized, setRecord]);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (!stopped) timer = setTimeout(() => { void poll(); }, pollDelay.current);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [enabled, refresh]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controller.current?.abort(); };
  }, []);

  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy]);

  const upload = async (file: File, contentType: UploadContentType, segments: VideoRange[]) => {
    if (controller.current) return;
    const active = new AbortController();
    controller.current = active;
    setBusy(true); setError(null); setProgress(null);
    const run = async () => {
      try {
        const env = sourceUploadEnv(api, segments);
        const options = { signal: active.signal, onProgress: (value: UploadProgress) => { if (mounted.current) setProgress(value); } };
        if (recordRef.current) await resumeUpload(env, recordRef.current, file, options);
        else {
          // Retry a lost create response with the same key while this draft remains in memory.
          const signature = `${await fingerprintBlob(file)}:${JSON.stringify(segments)}`;
          if (intent.current?.signature !== signature) intent.current = { signature, key: crypto.randomUUID() };
          await startUpload(env, { sessionId, partNo: 0, file, contentType, idempotencyKey: intent.current.key, onRecord: setRecord }, options);
        }
        setRecord(null);
      } catch (caught) { fail(caught); }
      finally {
        controller.current = null;
        await refresh();
        onChanged();
        if (mounted.current) setBusy(false);
      }
    };
    work.current = run();
    await work.current;
    work.current = null;
  };

  const stopTransfers = async () => { controller.current?.abort(); await work.current; };
  const abort = async () => {
    await stopTransfers();
    setBusy(true); setError(null);
    try {
      // A lost creation response still leaves a discoverable server attempt.
      const current = (await api.getVideoSource(sessionId)).source;
      const id = current?.upload_id ?? recordRef.current?.uploadId;
      if (id) await api.abortVideoUpload(id);
      setRecord(null); intent.current = null;
    } catch (caught) { fail(caught); }
    finally { await refresh(); onChanged(); if (mounted.current) setBusy(false); }
  };

  const pending = source === undefined || (source !== null && (
    source.state === 'open' || source.state === 'completing'
    || source.processing_state === 'pending' || source.processing_state === 'queued'
  ));
  return { source, record, error: error ?? pollError, busy, progress, upload, abort, refresh, stopTransfers,
    blocksFinish: enabled && (busy || pending),
    followupsAllowed: source?.state === 'accepted' && (source.processing_state === 'ready' || source.processing_state === 'failed'),
  };
}

export type SourceUploadController = ReturnType<typeof useSourceUpload>;
