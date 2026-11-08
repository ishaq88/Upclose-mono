import { useCallback, useEffect, useRef, useState } from 'react';
import type { Api, AudioAcceptedResponse, AudioContentType, EngineAudioSource } from '../api/client';
import { ApiError } from '../api/client';
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

/** Hex SHA-256 of the clip, which the source request carries so the server can verify the stored bytes. */
export async function sha256Hex(blob: Blob): Promise<string> {
  const bytes = typeof blob.arrayBuffer === 'function'
    ? await blob.arrayBuffer()
    : await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(new Error('The clip could not be read.'));
      reader.readAsArrayBuffer(blob);
    });
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** How the clip is labeled as evidence: a clip recorded now is live, a chosen file is a replay, a sample is synthetic. */
export interface AudioMeta { provenance: EngineAudioSource['provenance']; device: string }

export type AudioPhase = 'idle' | 'uploading' | 'completing' | 'attaching' | 'attached' | 'failed';

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
  const lastFile = useRef<{ file: File; contentType: AudioContentType; meta: AudioMeta } | null>(null);
  /** A completed upload whose source has not been attached yet; a retry then only repeats the attach. */
  const pendingAttach = useRef<{ uploadId: string; sha256: string; meta: AudioMeta } | null>(null);
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

  const run = useCallback(async (file: File, contentType: AudioContentType, meta: AudioMeta = { provenance: 'cached', device: 'audio-file' }) => {
    if (controller.current) return;
    const active = new AbortController();
    controller.current = active;
    lastFile.current = { file, contentType, meta };
    setError(null);
    setProgress(null);
    setPhase('uploading');
    work.current = (async () => {
      try {
        let pending = pendingAttach.current;
        if (!pending) {
          await releaseStale();
          await startUpload(audioUploadEnv(api, contentType), {
            sessionId, partNo: 0, file, contentType, idempotencyKey: crypto.randomUUID(),
            onRecord: (record) => { uploadId.current = record.uploadId; },
          }, {
            signal: active.signal,
            onProgress: (value) => { if (mounted.current) { setProgress(value); setPhase(value.phase); } },
          });
          pending = { uploadId: uploadId.current ?? '', sha256: await sha256Hex(file), meta };
          pendingAttach.current = pending;
          uploadId.current = null;
        }
        if (mounted.current) setPhase('attaching');
        try {
          await api.attachEngineAudio(sessionId, {
            kind: 'engine_audio', provenance: pending.meta.provenance, device: pending.meta.device,
            captured_at: new Date().toISOString(), data: { upload_id: pending.uploadId, sha256: pending.sha256 },
          });
        } catch (caught) {
          // 409: the server already finalized this clip as a source, so the listing below shows it.
          if (!(caught instanceof ApiError && caught.status === 409)) throw caught;
        }
        pendingAttach.current = null;
        lastFile.current = null;
        if (mounted.current) setPhase('attached');
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
    pendingAttach.current = null;
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
    if (last) await run(last.file, last.contentType, last.meta);
  }, [run]);

  const dismissError = useCallback(() => { setError(null); setPhase('idle'); }, []);

  return { phase, progress, error, busy: phase === 'uploading' || phase === 'completing' || phase === 'attaching', upload: run, abort, retry, dismissError, canRetry: phase === 'failed' && lastFile.current !== null };
}

export type AudioUploadController = ReturnType<typeof useAudioUpload>;
