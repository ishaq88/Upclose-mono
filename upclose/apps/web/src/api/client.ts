import type { components } from '../generated/contracts';
import { API_BASE } from '../config';

type Schemas = components['schemas'];
export type CaptureMode = Schemas['CreateSessionRequest']['capture_mode'];
export type VideoRange = Schemas['VideoRange'];
export type VideoSourceResponse = Schemas['VideoSourceResponse'];
export type VideoUploadResponse = Schemas['VideoUploadResponse'];
export type CreateVideoUploadRequest = Schemas['CreateVideoUploadRequest'];
export type CreateVideoUploadResponse = Schemas['CreateVideoUploadResponse'];
export type VideoAcceptedResponse = Schemas['VideoAcceptedResponse'];
export type SessionResponse = Schemas['SessionResponse'];
export type PartSummary = Schemas['PartSummary'];
export type PartState = Schemas['PartState'];
export type CreateSessionResponse = Schemas['CreateSessionResponse'];
export type CreateUploadRequest = Schemas['CreateUploadRequest'];
export type CreateUploadResponse = Schemas['CreateUploadResponse'];
export type UploadUrl = Schemas['UploadUrl'];
export type UploadResponse = Schemas['UploadResponse'];
export type PartAcceptedResponse = Schemas['PartAcceptedResponse'];
export type FinalizeResponse = Schemas['FinalizeResponse'];
export type SkipPartResponse = Schemas['SkipPartResponse'];
export type ProgressResponse = Schemas['ProgressResponse'];
export type Part1Verdict = Schemas['Part1Verdict'];
export type FreeFinding = Schemas['FreeFinding'];
export type PublicCoverage = Schemas['PublicCoverage'];
export type LockedZoneSummary = Schemas['LockedZoneSummary'];
export type ReportProgress = Schemas['ReportProgress'];
export type ReportState = Schemas['ReportState'];
export type CheckoutResponse = Schemas['CheckoutResponse'];
export type PaidReport = Schemas['PaidReport'];
export type FreeReport = Schemas['FreeReport'];
export type CategoryScores = Schemas['CategoryScores'];
export type ReportStatus = Schemas['ReportStatus'];
export type ReportMedia = 'narration' | 'audio' | 'spectrogram';
export type ShareEnableResponse = Schemas['ShareEnableResponse'];
export type ProblemCode = Schemas['Problem']['code'];
export type SourceKind = 'rig_telemetry' | 'obd_snapshot' | 'engine_audio';
export type SourceProvenance = Schemas['SourceRecord']['provenance'];
/** One evidence source attached to an inspection, as listed by GET /sessions/{id}/sources (no raw samples). */
export interface CaptureSource {
  source_id: string;
  kind: SourceKind;
  provenance: SourceProvenance;
  device: string;
  captured_at: string;
  data_sha256: string;
  summary: { state: 'assessed' | 'not_assessed'; flag_count?: number } & Record<string, unknown>;
}

/** The create response carries no chunk size: pieces are 8 MiB. */
export type CreateAudioUploadRequest = Schemas['CreateAudioUploadRequest'];
export type AudioContentType = CreateAudioUploadRequest['content_type'];
export type AudioUploadResponse = Schemas['AudioUploadResponse'];
export type AudioAcceptedResponse = Schemas['AudioAcceptedResponse'];

/** Spoken-guide catalog served by GET /voice-guide (PLACEHOLDER shape matching the current API; no generated type yet). */
export interface VoiceGuideLine { id: string; kind: string; zone: string | null; caption: string; approx_seconds: number }
export interface VoiceGuideCatalog { version: string; voice_label: string; provenance: string; lines: VoiceGuideLine[] }

export type ApiErrorCode = ProblemCode | 'NETWORK' | 'UNKNOWN';

/** A failed API call. `detail` comes from the server's problem document and carries no secrets. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly detail: string;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, code: ApiErrorCode, detail: string, retryAfterSeconds: number | null = null) {
    super(`${status} ${code}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** Parse a Retry-After header (delta seconds or HTTP date) into whole seconds, or null. */
export function parseRetryAfter(value: string | null, nowMs: number = Date.now()): number | null {
  if (value === null || value.trim() === '') return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
  const date = Date.parse(value);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - nowMs) / 1000));
}

const KNOWN_CODES: ReadonlySet<string> = new Set<ProblemCode>([
  'TOKEN_INVALID', 'NOT_FOUND', 'STATE_CONFLICT', 'UPLOAD_TOO_LARGE', 'UPLOAD_INCOMPLETE',
  'RATE_LIMITED', 'VALIDATION', 'REPORT_NOT_READY', 'INTERNAL',
]);

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ApiOptions {
  /** Read lazily so a token set after construction is used. Sent only as an Authorization header. */
  getToken: () => string | null;
  /** Defaults to the global fetch, resolved at call time. */
  fetchImpl?: FetchLike;
  baseUrl?: string;
  now?: () => number;
}

export interface Api {
  /** `accountToken` links the new inspection to a signed-in account; it is never stored as the owner token. */
  createSession(consentVersion: string, listingText: string | null, captureMode?: CaptureMode, accountToken?: string | null): Promise<CreateSessionResponse>;
  /** Owner (or linked account) report JSON: free, paid or still-building status. */
  getReport(slug: string): Promise<FreeReport | PaidReport | ReportStatus>;
  getReportMedia(slug: string, which: ReportMedia, signal?: AbortSignal): Promise<Blob>;
  /** Owner-only. Sets the vehicle name shown on the report and in My reports. */
  updateLabel(sessionId: string, label: string): Promise<SessionResponse>;
  /** Owner-only. Turns on the read-only share link for a paid report. */
  enableShare(slug: string): Promise<ShareEnableResponse>;
  disableShare(slug: string): Promise<void>;
  getVideoSource(sessionId: string): Promise<VideoSourceResponse>;
  createVideoUpload(sessionId: string, request: CreateVideoUploadRequest, idempotencyKey: string): Promise<CreateVideoUploadResponse>;
  videoUploadUrls(uploadId: string, partNumbers: readonly number[]): Promise<UploadUrl[]>;
  getVideoUpload(uploadId: string): Promise<VideoUploadResponse>;
  completeVideoUpload(uploadId: string): Promise<VideoAcceptedResponse>;
  abortVideoUpload(uploadId: string): Promise<void>;
  getSession(sessionId: string): Promise<SessionResponse>;
  createUpload(sessionId: string, request: CreateUploadRequest, idempotencyKey: string): Promise<CreateUploadResponse>;
  uploadUrls(uploadId: string, partNumbers: readonly number[]): Promise<UploadUrl[]>;
  getUpload(uploadId: string): Promise<UploadResponse>;
  completeUpload(uploadId: string): Promise<PartAcceptedResponse>;
  abortUpload(uploadId: string): Promise<void>;
  skipPart(partId: string): Promise<SkipPartResponse>;
  finalize(sessionId: string): Promise<FinalizeResponse>;
  /** Owner progress: server part states, the free Part-1 verdict (or null) and report state (or null). */
  getProgress(sessionId: string): Promise<ProgressResponse>;
  /** Owner-only. Creates a hosted Checkout; the response grants nothing. */
  createCheckout(slug: string): Promise<CheckoutResponse>;
  getReportHtml(slug: string, signal?: AbortSignal): Promise<string>;
  /** Owner-only approved evidence frame as a data URL (token never enters the URL). */
  getFrameDataUrl(path: string, signal?: AbortSignal): Promise<string>;
  deleteMedia(sessionId: string): Promise<void>;
  /** Owner-only. Evidence sources attached by capture kits. A 404 (an API without the route) means none yet. */
  listSources(sessionId: string): Promise<CaptureSource[]>;
  createAudioUpload(sessionId: string, request: CreateAudioUploadRequest): Promise<AudioUploadResponse>;
  audioUploadUrls(uploadId: string, partNumbers: readonly number[]): Promise<UploadUrl[]>;
  completeAudioUpload(uploadId: string): Promise<AudioAcceptedResponse>;
  abortAudioUpload(uploadId: string): Promise<void>;
  /** Owner-only. Fixed catalog of spoken guidance lines with captions. */
  getVoiceGuide(): Promise<VoiceGuideCatalog>;
  /** Owner-only. Audio for one catalog line; the token goes in a header, so the bytes come back as a Blob. */
  getVoiceGuideAudio(lineId: string, signal?: AbortSignal): Promise<Blob>;
}

interface RequestOptions {
  body?: unknown;
  idempotencyKey?: string;
  authenticated?: boolean;
  /** Overrides the owner token for this call (used only to send an account token). */
  bearer?: string;
}

export function createApi(options: ApiOptions): Api {
  const base = options.baseUrl ?? API_BASE;
  const now = options.now ?? Date.now;
  const doFetch: FetchLike = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  const checkoutKeys = new Map<string, string>();
  let checkoutOwner: string | null | undefined;

  async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey;
    if (opts.bearer) headers.Authorization = `Bearer ${opts.bearer}`;
    else if (opts.authenticated !== false) {
      const token = options.getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
    }
    let response: Response;
    try {
      response = await doFetch(`${base}${path}`, {
        method,
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
        cache: 'no-store',
      });
    } catch {
      throw new ApiError(0, 'NETWORK', 'The server could not be reached.');
    }
    if (!response.ok) throw await toApiError(response, now());
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  return {
    getReportMedia: async (slug, which, signal) => {
      const token = options.getToken();
      const path = which === 'narration' ? 'narration' : `sources/engine_audio/${which}`;
      let response: Response;
      try {
        response = await doFetch(`${base}/reports/${encodeURIComponent(slug)}/${path}`, {
          headers: { Accept: which === 'spectrogram' ? 'image/*' : 'audio/*', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', redirect: 'error', signal,
        });
      } catch { throw new ApiError(0, 'NETWORK', 'The report media could not be reached.'); }
      if (!response.ok) throw await toApiError(response, now());
      return response.blob();
    },
    getReportHtml: async (slug, signal) => {
      const token = options.getToken();
      let response: Response;
      try {
        response = await doFetch(`${base.replace(/\/api\/v1\/?$/, '')}/r/${encodeURIComponent(slug)}`, {
          headers: { Accept: 'text/html', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', signal, redirect: 'error',
        });
      } catch { throw new ApiError(0, 'NETWORK', 'The report could not be reached.'); }
      if (!response.ok) throw await toApiError(response, now());
      if (response.status !== 200 || !response.headers.get('Content-Type')?.includes('text/html')) {
        throw new ApiError(409, 'REPORT_NOT_READY', 'The report is not ready.');
      }
      return response.text();
    },
    getFrameDataUrl: async (path, signal) => {
      const token = options.getToken();
      let response: Response;
      try {
        response = await doFetch(path, {
          headers: { Accept: 'image/*', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', signal,
        });
      } catch { throw new ApiError(0, 'NETWORK', 'The evidence frame could not be reached.'); }
      if (!response.ok) throw await toApiError(response, now());
      const blob = await response.blob();
      return await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new ApiError(0, 'NETWORK', 'The evidence frame could not be read.'));
        reader.readAsDataURL(blob);
      });
    },
    deleteMedia: async (id) => { await request('POST', `/sessions/${id}/delete-media`, { body: {} }); },
    listSources: async (id) => {
      try {
        const result = await request<{ sources: CaptureSource[] }>('GET', `/sessions/${id}/sources`);
        return result.sources;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return [];
        throw error;
      }
    },
    createAudioUpload: (id, body) => request('POST', `/sessions/${id}/audio`, { body }),
    audioUploadUrls: async (id, numbers) => {
      const result = await request<{ items: UploadUrl[] }>('POST', `/audio/${id}/urls`, { body: { part_numbers: [...numbers] } });
      return result.items;
    },
    completeAudioUpload: (id) => request('POST', `/audio/${id}/complete`, { body: {} }),
    abortAudioUpload: async (id) => { await request('DELETE', `/audio/${id}`); },
    getVoiceGuide: () => request('GET', '/voice-guide'),
    getVoiceGuideAudio: async (lineId, signal) => {
      const token = options.getToken();
      let response: Response;
      try {
        response = await doFetch(`${base}/voice-guide/${encodeURIComponent(lineId)}`, {
          headers: { Accept: 'audio/*', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', signal,
        });
      } catch { throw new ApiError(0, 'NETWORK', 'The voice guide could not be reached.'); }
      if (!response.ok) throw await toApiError(response, now());
      return response.blob();
    },
    getReport: (slug) => request('GET', `/reports/${encodeURIComponent(slug)}`),
    updateLabel: (id, label) => request('PUT', `/sessions/${id}/label`, { body: { label } }),
    enableShare: (slug) => request('POST', `/reports/${encodeURIComponent(slug)}/share`, { body: {} }),
    disableShare: async (slug) => { await request('DELETE', `/reports/${encodeURIComponent(slug)}/share`); },
    createSession: (consentVersion, listingText, captureMode, accountToken) => request('POST', '/sessions', {
      authenticated: false,
      ...(accountToken ? { bearer: accountToken } : {}),
      body: {
        ...(captureMode && captureMode !== 'guided' ? { capture_mode: captureMode } : {}),
        purpose: 'buyer', consent_version: consentVersion, consent: true,
        ...(listingText ? { listing_text: listingText } : {}),
      },
    }),
    getVideoSource: (id) => request('GET', `/sessions/${id}/video`),
    createVideoUpload: (id, body, idempotencyKey) => request('POST', `/sessions/${id}/video`, { body, idempotencyKey }),
    videoUploadUrls: async (id, numbers) => {
      const result = await request<{ items: UploadUrl[] }>('POST', `/video-uploads/${id}/urls`, { body: { part_numbers: [...numbers] } });
      return result.items;
    },
    getVideoUpload: (id) => request('GET', `/video-uploads/${id}`),
    completeVideoUpload: (id) => request('POST', `/video-uploads/${id}/complete`, { body: {} }),
    abortVideoUpload: (id) => request('DELETE', `/video-uploads/${id}`),
    getSession: (id) => request('GET', `/sessions/${id}`),
    createUpload: (sessionId, body, idempotencyKey) =>
      request('POST', `/sessions/${sessionId}/parts`, { body, idempotencyKey }),
    uploadUrls: async (uploadId, partNumbers) => {
      const result = await request<{ items: UploadUrl[] }>('POST', `/uploads/${uploadId}/urls`,
        { body: { part_numbers: [...partNumbers] } });
      return result.items;
    },
    getUpload: (id) => request('GET', `/uploads/${id}`),
    completeUpload: (id) => request('POST', `/uploads/${id}/complete`, { body: {} }),
    abortUpload: (id) => request('DELETE', `/uploads/${id}`),
    skipPart: (partId) => request('POST', `/parts/${partId}/skip`, { body: {} }),
    finalize: (sessionId) => request('POST', `/sessions/${sessionId}/finalize`, { body: {} }),
    getProgress: (sessionId) => request('GET', `/sessions/${sessionId}/progress`),
    createCheckout: async (slug) => {
      const token = options.getToken();
      if (checkoutOwner !== token) {
        checkoutKeys.clear();
        checkoutOwner = token;
      }
      const key = checkoutKeys.get(slug) ?? crypto.randomUUID();
      checkoutKeys.set(slug, key);
      try {
        const result = await request<CheckoutResponse>('POST', `/reports/${encodeURIComponent(slug)}/checkout`, {
          body: { product: 'report_unlock' }, idempotencyKey: key,
        });
        checkoutKeys.delete(slug);
        return result;
      } catch (error) {
        // Keep the key across a lost response; a terminal conflict starts a new attempt.
        if (error instanceof ApiError && error.status === 409) checkoutKeys.delete(slug);
        throw error;
      }
    },
  };
}

async function toApiError(response: Response, nowMs: number): Promise<ApiError> {
  let code: ApiErrorCode = 'UNKNOWN';
  let detail = '';
  try {
    const problem = (await response.json()) as { code?: unknown; detail?: unknown };
    if (typeof problem.code === 'string' && KNOWN_CODES.has(problem.code)) code = problem.code as ProblemCode;
    if (typeof problem.detail === 'string') detail = problem.detail;
  } catch {
    // Non-JSON error bodies (proxy errors) are classified by status alone.
  }
  if (code === 'UNKNOWN' && response.status === 401) code = 'TOKEN_INVALID';
  return new ApiError(response.status, code, detail, parseRetryAfter(response.headers.get('Retry-After'), nowMs));
}
