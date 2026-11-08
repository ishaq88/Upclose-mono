import { useCallback, useEffect, useRef, useState } from 'react';
import type { Api, AudioAcceptedResponse, AudioContentType } from '../api/client';
import { describeError, type UiError } from '../errors';
import { abortableSleep, DEFAULT_CHUNK_SIZE_BYTES, isUploadAbort, startUpload, type UploadEnv, type UploadProgress } from './multipart';

/** Mirrors the API contract: clips are at most 10 MiB and 120 s. The server stays the authority. */
export const MAX_AUDIO_BYTES = 10 * 1024 * 1024;
export const MAX_AUDIO_SECONDS = 120;

const BY_EXTENSION: Record<string, AudioContentType> = { wav: 'audio/wav', mp3: 'audio/mpeg', m4a: 'audio/x-m4a', mp4: 'audio/mp4' };
const BY_MIME: Record<string, AudioContentType> = {
  'audio/wav': 'audio/wav', 'audio/x-wav': 'audio/x-wav', 'audio/wave': 'audio/wav', 'audio/vnd.wave': 'audio/wav',
  'audio/mpeg': 'audio/mpeg', 'audio/mp3': 'audio/mpeg', 'audio/mp4': 'audio/mp4', 'audio/x-m4a': 'audio/x-m4a', 'audio/m4a': 'audio/x-m4a',
};

/** The accepted type for a picked or recorded file (WAV, MP3, M4A), from its MIME type or, if blank, its extension. */
export function normalizeAudioType(file: { type: string; name?: string }): AudioContentType | null {
  const mime = file.type.split(';')[0]!.trim().toLowerCase();
  if (mime && BY_MIME[mime]) return BY_MIME[mime]!;
  if (mime && mime !== 'application/octet-stream') return null;
  const extension = /\.([a-z0-9]+)$/i.exec(file.name ?? '')?.[1]?.toLowerCase();
  return extension ? (BY_EXTENSION[extension] ?? null) : null;
}

export type AudioCheck = { ok: true; contentType: AudioContentType; notice: string | null } | { ok: false; message: string };

/** Early, local rejection with a plain message. `durationSeconds` is null when the browser could not read it. */
export function checkAudioFile(file: { type: string; name?: string; size: number }, durationSeconds: number | null): AudioCheck {
  const contentType = normalizeAudioType(file);
  if (contentType === null) return { ok: false, message: 'This file type is not supported. Use a WAV, MP3 or M4A recording.' };
  if (file.size === 0) return { ok: false, message: 'This file is empty. Record or choose another clip.' };
  if (file.size > MAX_AUDIO_BYTES) return { ok: false, message: `This clip is larger than ${MAX_AUDIO_BYTES / (1024 * 1024)} MiB, which is the limit for engine audio. Record a shorter clip.` };
  if (durationSeconds !== null && durationSeconds > MAX_AUDIO_SECONDS) {
    return { ok: false, message: `This clip is about ${Math.round(durationSeconds)} seconds. Engine audio can be at most ${MAX_AUDIO_SECONDS} seconds. Record a shorter clip.` };
  }
  return { ok: true, contentType, notice: durationSeconds === null ? 'This browser could not read the clip length. You can still upload it; the server checks it again.' : null };
}

/** Read the duration with a throwaway audio element. Resolves null on any failure or timeout. */
export function readAudioDuration(blob: Blob, timeoutMs = 5000): Promise<number | null> {
  if (typeof document === 'undefined' || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') return Promise.resolve(null);
  return new Promise((resolve) => {
    const audio = document.createElement('audio');
    const url = URL.createObjectURL(blob);
    let settled = false;
    const finish = (value: number | null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      audio.removeAttribute('src');
      audio.load();
      URL.revokeObjectURL(url);
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    audio.preload = 'metadata';
    audio.onloadedmetadata = () => finish(Number.isFinite(audio.duration) ? audio.duration : null);
    audio.onerror = () => finish(null);
    audio.src = url;
  });
}

/** Endpoint selection for the shared piece/retry protocol. The API plans 8 MiB pieces and returns only the count. */
export function audioUploadEnv(api: Api, contentType: AudioContentType): UploadEnv<AudioAcceptedResponse> {
  return {
    api: {
      createUpload: async (id, request) => {
        const created = await api.createAudioUpload(id, { content_type: contentType, content_length: request.content_length });
        return { upload_id: created.upload_id, chunk_size_bytes: DEFAULT_CHUNK_SIZE_BYTES, chunk_count: created.chunk_count };
      },
      uploadUrls: api.audioUploadUrls,
      // The audio API has no piece listing; an "incomplete" recovery re-sends every piece of the open attempt.
      getUpload: async () => ({ state: 'open', pieces: [] }),
      completeUpload: api.completeAudioUpload,
      abortUpload: api.abortAudioUpload,
    },
    fetchImpl: (input, init) => globalThis.fetch(input, init),
    sleep: abortableSleep, now: Date.now, random: Math.random,
  };
}

export type AudioPhase = 'idle' | 'uploading' | 'completing' | 'accepted' | 'failed';

interface Options { api: Api; sessionId: string; onChanged: () => void; onUnauthorized: () => void }

/**
 * One engine-audio upload at a time: start, progress, abort, retry. A retry (or a new clip after a failure) first
 * releases the stale server attempt, because the API allows one open attempt per session.
 */
export function useAudioUpload({ api, sessionId, onChanged, onUnauthorized }: Options) {
  const [phase, setPhase] = useState<AudioPhase>('idle');
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const controller = useRef<AbortController | null>(null);
  const work = useRef<Promise<void> | null>(null);
  const uploadId = useRef<string | null>(null);
  const lastFile = useRef<{ file: File; contentType: AudioContentType } | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controller.current?.abort(); };
  }, []);

  const releaseStale = useCallback(async () => {
    const id = uploadId.current;
    uploadId.current = null;
    if (id) {
      try { await api.abortAudioUpload(id); } catch { /* already closed */ }
    }
  }, [api]);

  const run = useCallback(async (file: File, contentType: AudioContentType) => {
    if (controller.current) return;
    const active = new AbortController();
    controller.current = active;
    lastFile.current = { file, contentType };
    setError(null);
    setProgress(null);
    setPhase('uploading');
    work.current = (async () => {
      try {
        await releaseStale();
        await startUpload(audioUploadEnv(api, contentType), {
          sessionId, partNo: 0, file, contentType, idempotencyKey: crypto.randomUUID(),
          onRecord: (record) => { uploadId.current = record.uploadId; },
        }, {
          signal: active.signal,
          onProgress: (value) => { if (mounted.current) { setProgress(value); setPhase(value.phase); } },
        });
        uploadId.current = null;
        lastFile.current = null;
        if (mounted.current) setPhase('accepted');
      } catch (caught) {
        if (mounted.current) {
          const described = describeError(caught);
          if (described.action === 'new_session') onUnauthorized();
          if (isUploadAbort(caught)) {
            setPhase('idle');
          } else {
            setError(described);
            setPhase('failed');
          }
        }
      } finally {
        controller.current = null;
        onChanged();
      }
    })();
    await work.current;
    work.current = null;
  }, [api, sessionId, onChanged, onUnauthorized, releaseStale]);

  const abort = useCallback(async () => {
    controller.current?.abort();
    await work.current;
    await releaseStale();
    lastFile.current = null;
    if (mounted.current) {
      setPhase('idle');
      setProgress(null);
      setError(null);
    }
  }, [releaseStale]);

  const retry = useCallback(async () => {
    const last = lastFile.current;
    if (last) await run(last.file, last.contentType);
  }, [run]);

  const dismissError = useCallback(() => { setError(null); setPhase('idle'); }, []);

  return { phase, progress, error, busy: phase === 'uploading' || phase === 'completing', upload: run, abort, retry, dismissError, canRetry: phase === 'failed' && lastFile.current !== null };
}

export type AudioUploadController = ReturnType<typeof useAudioUpload>;
