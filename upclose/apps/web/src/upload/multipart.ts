/**
 * Browser multipart upload client for the UpClose API (implementation contract section 3).
 *
 * Pure of React and of globals: every effect (API, storage PUT, timers, randomness) is injected so the
 * behaviour is unit-testable with a mocked fetch. The browser sets Content-Length itself; this module
 * never sets it. Presigned URLs are secrets and are never logged, persisted or put in error messages.
 */
import { ApiError, type Api, type FetchLike, type PartAcceptedResponse, type UploadResponse, type UploadUrl } from '../api/client';
import { MAX_URLS_PER_REQUEST } from '../config';

export const DEFAULT_CHUNK_SIZE_BYTES = 8 * 1024 * 1024;

export type UploadContentType = 'video/mp4' | 'video/webm' | 'video/quicktime';

export type UploadSource = Blob & { readonly name?: string; readonly lastModified?: number };

/** Non-secret facts needed to resume after a reload. The user must reselect the same file. */
export interface UploadRecord {
  uploadId: string;
  partId: string;
  partNo: number;
  contentType: UploadContentType;
  size: number;
  chunkSize: number;
  chunkCount: number;
  fileName: string;
  lastModified: number;
  fingerprint: string;
}

export interface UploadProgress {
  phase: 'uploading' | 'completing';
  uploadedBytes: number;
  totalBytes: number;
  donePieces: number;
  totalPieces: number;
}

export interface UploadEnv<T = PartAcceptedResponse> {
  api: {
    createUpload: (sessionId: string, request: Parameters<Api['createUpload']>[1], key: string) => Promise<{ upload_id: string; part_id?: string; chunk_size_bytes: number; chunk_count: number }>;
    uploadUrls: Api['uploadUrls'];
    getUpload: (id: string) => Promise<Pick<UploadResponse, 'state' | 'pieces'>>;
    completeUpload: (id: string) => Promise<T>;
    abortUpload: Api['abortUpload'];
  };
  /** Used only for PUTs to presigned storage URLs. */
  fetchImpl: FetchLike;
  sleep: (ms: number, signal?: AbortSignal) => Promise<void>;
  now: () => number;
  random: () => number;
}

export interface UploadOptions {
  concurrency: number;
  maxPieceAttempts: number;
  maxUrlRefreshesPerPiece: number;
  maxRateLimitWaits: number;
  maxCompleteRecoveries: number;
  maxCompleteAttempts: number;
  backoffBaseMs: number;
  backoffMaxMs: number;
  /** A cached URL expiring within this window is refreshed before use. */
  urlSkewMs: number;
  maxRetryAfterSeconds: number;
  signal?: AbortSignal;
  onProgress?: (progress: UploadProgress) => void;
}

export const DEFAULT_OPTIONS: UploadOptions = {
  concurrency: 3,
  maxPieceAttempts: 5,
  maxUrlRefreshesPerPiece: 4,
  maxRateLimitWaits: 4,
  maxCompleteRecoveries: 2,
  maxCompleteAttempts: 3,
  backoffBaseMs: 500,
  backoffMaxMs: 8_000,
  urlSkewMs: 15_000,
  maxRetryAfterSeconds: 120,
};

export type UploadFailureKind = 'piece' | 'rate_limited' | 'wrong_file' | 'gone' | 'aborted';

export class UploadError extends Error {
  readonly kind: UploadFailureKind;
  readonly retryAfterSeconds: number | null;

  constructor(kind: UploadFailureKind, message: string, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = 'UploadError';
    this.kind = kind;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export function isUploadAbort(error: unknown): boolean {
  return error instanceof UploadError && error.kind === 'aborted';
}

// --- layout ---------------------------------------------------------------------------------

export function pieceCount(totalBytes: number, chunkSize: number): number {
  return Math.ceil(totalBytes / chunkSize);
}

/** Byte range `[start, end)` of piece `partNumber` (1-based). Every piece is a full chunk except the last. */
export function pieceRange(totalBytes: number, chunkSize: number, partNumber: number): { start: number; end: number } {
  const start = (partNumber - 1) * chunkSize;
  return { start, end: Math.min(start + chunkSize, totalBytes) };
}

export function pieceSize(totalBytes: number, chunkSize: number, partNumber: number): number {
  const { start, end } = pieceRange(totalBytes, chunkSize, partNumber);
  return end - start;
}

/** Piece numbers the server has not listed with exactly the expected size. */
export function missingPieces(
  record: Pick<UploadRecord, 'size' | 'chunkSize' | 'chunkCount'>, listed: UploadResponse['pieces'],
): number[] {
  const accepted = new Map(listed.map((piece) => [piece.part_number, piece.size_bytes]));
  const missing: number[] = [];
  for (let n = 1; n <= record.chunkCount; n += 1) {
    if (accepted.get(n) !== pieceSize(record.size, record.chunkSize, n)) missing.push(n);
  }
  return missing;
}

// --- cheap fingerprint ----------------------------------------------------------------------

const SAMPLE_BYTES = 64 * 1024;

async function readBytes(blob: Blob): Promise<Uint8Array> {
  if (typeof blob.arrayBuffer === 'function') return new Uint8Array(await blob.arrayBuffer());
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

function fnv1a(bytes: Uint8Array, seed: number): number {
  let hash = seed >>> 0;
  for (const byte of bytes) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash;
}

/**
 * Size plus FNV-1a hashes of the first, middle and last 64 KiB. Cheap and not cryptographic: it catches
 * "wrong file" on resume, not tampering. The server independently verifies sizes of every piece it keeps.
 */
export async function fingerprintBlob(blob: Blob): Promise<string> {
  const size = blob.size;
  const middle = Math.max(0, Math.floor(size / 2) - SAMPLE_BYTES / 2);
  const windows: ReadonlyArray<readonly [number, number]> = [
    [0, SAMPLE_BYTES], [middle, middle + SAMPLE_BYTES], [Math.max(0, size - SAMPLE_BYTES), size],
  ];
  let h1 = 0x811c9dc5;
  let h2 = 0x9747b28c;
  for (const [from, to] of windows) {
    const bytes = await readBytes(blob.slice(from, Math.min(to, size)));
    h1 = fnv1a(bytes, h1);
    h2 = fnv1a(bytes, h2 ^ 0x5bd1e995);
  }
  return `${size}:${h1.toString(16).padStart(8, '0')}${h2.toString(16).padStart(8, '0')}`;
}

/** Throw `wrong_file` unless the reselected file plausibly is the one the attempt was created for. */
export async function verifyResumeFile(record: UploadRecord, file: UploadSource): Promise<void> {
  if (file.size !== record.size) {
    throw new UploadError('wrong_file', 'That file is not the one this upload started with (different size).');
  }
  if (record.fingerprint && (await fingerprintBlob(file)) !== record.fingerprint) {
    throw new UploadError('wrong_file', 'That file is not the one this upload started with (contents differ).');
  }
}

// --- retry plumbing -------------------------------------------------------------------------

function backoffMs(attempt: number, env: UploadEnv<unknown>, options: UploadOptions): number {
  const exponential = Math.min(options.backoffMaxMs, options.backoffBaseMs * 2 ** (attempt - 1));
  return Math.round(exponential * (0.5 + env.random() * 0.5));
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new UploadError('aborted', 'The upload was cancelled.');
}

function isTransientApiError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

interface CachedUrl {
  url: string;
  headers: Record<string, string>;
  expiresAtMs: number;
}

function toCached(item: UploadUrl): CachedUrl {
  const parsed = Date.parse(item.expires_at);
  return { url: item.url, headers: item.headers, expiresAtMs: Number.isNaN(parsed) ? 0 : parsed };
}

/** Headers the browser must own are dropped; the server currently returns none. */
function safeHeaders(headers: Record<string, string>): Record<string, string> {
  const forbidden = new Set(['content-length', 'host', 'connection', 'transfer-encoding']);
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !forbidden.has(name.toLowerCase())));
}

function mapApiFailure(error: unknown): unknown {
  if (error instanceof ApiError && (error.status === 404 || error.status === 409) && error.code !== 'UPLOAD_INCOMPLETE') {
    return new UploadError('gone', 'This upload attempt is no longer open.');
  }
  return error;
}

/** Serves presigned URLs, batching <=10 per request and refreshing near/after expiry. */
class UrlPool {
  private readonly cache = new Map<number, CachedUrl>();
  private inflight: Promise<void> | null = null;

  constructor(
    private readonly uploadId: string,
    private readonly env: UploadEnv<unknown>,
    private readonly options: UploadOptions,
    private readonly pending: () => number[],
  ) {}

  invalidate(partNumber: number): void {
    this.cache.delete(partNumber);
  }

  private isFresh(entry: CachedUrl | undefined): entry is CachedUrl {
    return entry !== undefined && entry.expiresAtMs - this.env.now() > this.options.urlSkewMs;
  }

  async get(partNumber: number): Promise<CachedUrl> {
    for (;;) {
      throwIfAborted(this.options.signal);
      const cached = this.cache.get(partNumber);
      if (this.isFresh(cached)) return cached;
      if (this.inflight) {
        await this.inflight;
        continue;
      }
      const request = this.fetchBatch(partNumber).finally(() => { this.inflight = null; });
      this.inflight = request;
      await request;
      const issued = this.cache.get(partNumber);
      if (issued === undefined) throw new UploadError('piece', 'The server did not issue an upload address.');
      if (!this.isFresh(issued)) throw new UploadError('piece', 'The server issued an already-expired upload address.');
    }
  }

  private batchFor(partNumber: number): number[] {
    const wanted = [partNumber];
    for (const candidate of this.pending()) {
      if (wanted.length >= MAX_URLS_PER_REQUEST) break;
      if (candidate !== partNumber && !this.isFresh(this.cache.get(candidate))) wanted.push(candidate);
    }
    return wanted;
  }

  private async fetchBatch(partNumber: number): Promise<void> {
    const wanted = this.batchFor(partNumber);
    for (let waits = 0; ; waits += 1) {
      throwIfAborted(this.options.signal);
      try {
        const items = await this.env.api.uploadUrls(this.uploadId, wanted);
        for (const item of items) this.cache.set(item.part_number, toCached(item));
        return;
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 429) throw mapApiFailure(error);
        const seconds = error.retryAfterSeconds ?? 5;
        if (seconds > this.options.maxRetryAfterSeconds || waits >= this.options.maxRateLimitWaits) {
          throw new UploadError('rate_limited', 'Too many address requests; try again shortly.', seconds);
        }
        await this.env.sleep(Math.max(1, seconds) * 1000, this.options.signal);
      }
    }
  }
}

// --- transfer -------------------------------------------------------------------------------

interface TransferJob<T = unknown> {
  env: UploadEnv<T>;
  options: UploadOptions;
  record: UploadRecord;
  file: UploadSource;
  /** Pieces already confirmed by the server before this transfer (for progress). */
  alreadyAccepted: ReadonlySet<number>;
}

async function putPiece(job: TransferJob, pool: UrlPool, partNumber: number, signal: AbortSignal): Promise<void> {
  const { env, options, record, file } = job;
  const { start, end } = pieceRange(record.size, record.chunkSize, partNumber);
  let attempts = 0;
  let refreshes = 0;
  for (;;) {
    throwIfAborted(signal);
    const target = await pool.get(partNumber);
    let status = 0;
    try {
      const response = await env.fetchImpl(target.url, {
        method: 'PUT', body: file.slice(start, end), headers: safeHeaders(target.headers),
        credentials: 'omit', referrerPolicy: 'no-referrer', signal,
      });
      status = response.status;
      if (response.ok) return;
    } catch {
      throwIfAborted(signal);
      status = 0; // network failure: retry with backoff
    }
    if (status === 403) {
      // Expired or rejected address: mint a fresh one for the same open upload (rate-limited server-side).
      refreshes += 1;
      if (refreshes > options.maxUrlRefreshesPerPiece) {
        throw new UploadError('piece', `Piece ${partNumber} was rejected by storage.`);
      }
      pool.invalidate(partNumber);
      continue;
    }
    if (status === 400 || status === 404) throw new UploadError('gone', `Storage refused piece ${partNumber}.`);
    attempts += 1;
    if (attempts >= options.maxPieceAttempts) {
      throw new UploadError('piece', `Piece ${partNumber} could not be uploaded.`);
    }
    await env.sleep(backoffMs(attempts, env, options), signal);
  }
}

/** Upload exactly `numbers` with bounded concurrency; pieces not listed are never touched. */
async function transferPieces(job: TransferJob, numbers: readonly number[]): Promise<void> {
  const { options, record } = job;
  throwIfAborted(options.signal);
  const queue = [...numbers];
  const done = new Set(job.alreadyAccepted);
  let uploadedBytes = [...done].reduce((sum, n) => sum + pieceSize(record.size, record.chunkSize, n), 0);
  const pool = new UrlPool(record.uploadId, job.env, options, () => queue.filter((n) => !done.has(n)));

  const internal = new AbortController();
  const onOuterAbort = (): void => internal.abort();
  options.signal?.addEventListener('abort', onOuterAbort, { once: true });
  let failure: unknown = null;

  const worker = async (): Promise<void> => {
    while (failure === null) {
      const next = queue.shift();
      if (next === undefined) return;
      try {
        await putPiece(job, pool, next, internal.signal);
      } catch (error) {
        failure ??= error;
        internal.abort(); // stop siblings promptly; the first failure is the one reported
        return;
      }
      done.add(next);
      uploadedBytes += pieceSize(record.size, record.chunkSize, next);
      options.onProgress?.({
        phase: 'uploading', uploadedBytes, totalBytes: record.size, donePieces: done.size, totalPieces: record.chunkCount,
      });
    }
  };

  try {
    const workers = Math.max(1, Math.min(options.concurrency, queue.length));
    await Promise.all(Array.from({ length: workers }, worker));
  } finally {
    options.signal?.removeEventListener('abort', onOuterAbort);
  }
  if (options.signal?.aborted) throw new UploadError('aborted', 'The upload was cancelled.');
  if (failure !== null) throw failure;
}

async function completeOnce<T>(env: UploadEnv<T>, options: UploadOptions, uploadId: string): Promise<T> {
  for (let attempt = 1; ; attempt += 1) {
    throwIfAborted(options.signal);
    try {
      return await env.api.completeUpload(uploadId);
    } catch (error) {
      // Completion is idempotent server-side, so a transient failure is safe to repeat.
      if (!isTransientApiError(error) || attempt >= options.maxCompleteAttempts) throw error;
      await env.sleep(backoffMs(attempt, env, options), options.signal);
    }
  }
}

/** Complete; on 409 UPLOAD_INCOMPLETE re-list, re-send only what storage lacks, and try again. */
async function completeWithRecovery<T>(job: TransferJob<T>): Promise<T> {
  const { env, options, record } = job;
  for (let recovery = 0; ; recovery += 1) {
    options.onProgress?.({
      phase: 'completing', uploadedBytes: record.size, totalBytes: record.size,
      donePieces: record.chunkCount, totalPieces: record.chunkCount,
    });
    try {
      return await completeOnce(env, options, record.uploadId);
    } catch (error) {
      const incomplete = error instanceof ApiError && error.status === 409 && error.code === 'UPLOAD_INCOMPLETE';
      if (!incomplete || recovery >= options.maxCompleteRecoveries) throw mapApiFailure(error);
      const listing = await env.api.getUpload(record.uploadId);
      if (listing.state !== 'open') throw new UploadError('gone', 'This upload attempt is no longer open.');
      const missing = missingPieces(record, listing.pieces);
      if (missing.length === 0) continue; // storage may have caught up; try completing again
      const accepted = new Set(listing.pieces.map((piece) => piece.part_number));
      await transferPieces({ ...job, alreadyAccepted: accepted }, missing);
    }
  }
}

// --- public operations ----------------------------------------------------------------------

export interface StartUploadInput {
  sessionId: string;
  partNo: number;
  file: UploadSource;
  contentType: UploadContentType;
  /** Reuse the same key when retrying creation so a lost response replays the original result. */
  idempotencyKey: string;
  /** Called as soon as the attempt exists so the caller can persist it for resume. */
  onRecord?: (record: UploadRecord) => void;
}

export async function startUpload<T>(
  env: UploadEnv<T>, input: StartUploadInput, overrides: Partial<UploadOptions> = {},
): Promise<T> {
  const options = { ...DEFAULT_OPTIONS, ...overrides };
  throwIfAborted(options.signal);
  const { file } = input;
  const created = await env.api.createUpload(input.sessionId,
    { part_no: input.partNo, content_type: input.contentType, content_length: file.size }, input.idempotencyKey);
  const chunkSize = created.chunk_size_bytes;
  const record: UploadRecord = {
    uploadId: created.upload_id, partId: created.part_id ?? '', partNo: input.partNo, contentType: input.contentType,
    size: file.size, chunkSize, chunkCount: created.chunk_count, fileName: file.name ?? '',
    lastModified: file.lastModified ?? 0, fingerprint: await fingerprintBlob(file),
  };
  input.onRecord?.(record);
  if (created.chunk_count !== pieceCount(file.size, chunkSize)) {
    throw new UploadError('gone', 'The server planned a different number of pieces than expected.');
  }
  const all = Array.from({ length: record.chunkCount }, (_, i) => i + 1);
  const job: TransferJob<T> = { env, options, record, file, alreadyAccepted: new Set() };
  await transferPieces(job, all);
  return completeWithRecovery(job);
}

/**
 * Continue an attempt after a reload. The caller must supply the same file; it is checked against the
 * recorded size and fingerprint and against the sizes of pieces storage already holds.
 */
export async function resumeUpload<T>(
  env: UploadEnv<T>, record: UploadRecord, file: UploadSource, overrides: Partial<UploadOptions> = {},
): Promise<T> {
  const options = { ...DEFAULT_OPTIONS, ...overrides };
  throwIfAborted(options.signal);
  await verifyResumeFile(record, file);
  let listing: Pick<UploadResponse, 'state' | 'pieces'>;
  try {
    listing = await env.api.getUpload(record.uploadId);
  } catch (error) {
    throw mapApiFailure(error);
  }
  if (listing.state === 'aborted') {
    throw new UploadError('gone', 'This upload attempt was cancelled; start the part again.');
  }
  const job: TransferJob<T> = {
    env, options, record, file, alreadyAccepted: new Set(listing.pieces.map((piece) => piece.part_number)),
  };
  if (listing.state === 'open') {
    for (const piece of listing.pieces) {
      if (piece.size_bytes !== pieceSize(record.size, record.chunkSize, piece.part_number)) {
        throw new UploadError('wrong_file', 'Pieces already uploaded do not match this file.');
      }
    }
    await transferPieces(job, missingPieces(record, listing.pieces));
  }
  // `completing` and `accepted` are settled server-side; completing again returns status, never a second object.
  return completeWithRecovery(job);
}

/** Retake: stop in-flight work first (abort the signal), then release the server-side attempt. Idempotent. */
export async function abortAttempt(env: Pick<UploadEnv, 'api'>, uploadId: string): Promise<void> {
  await env.api.abortUpload(uploadId);
}

/** Browser-safe sleep that rejects with an abort error when cancelled. */
export function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new UploadError('aborted', 'The upload was cancelled.'));
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new UploadError('aborted', 'The upload was cancelled.'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}
