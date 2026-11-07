/**
 * In-memory stand-in for the UpClose API and its object storage, for tests only (never imported by the app).
 * It enforces the documented rules that matter to the client: bearer auth, <=10 URLs per request, URL refresh
 * rate limit, expiring presigned URLs (403), exact piece sizes at completion, and Retry-After on 429.
 */
import type { CaptureSource, Part1Verdict, PartSummary, ReportProgress, VideoUploadResponse, CreateVideoUploadRequest } from '../api/client';

const CHUNK = 8 * 1024 * 1024;
const ZONES = ['driver_side', 'passenger_side', 'front_rear_body', 'tires', 'dash_electrical', 'engine_bay', 'underbody', 'interior'] as const;

export interface RecordedCall {
  method: string;
  url: string;
  body?: unknown;
  headers: Record<string, string>;
}

interface UploadRow {
  id: string;
  partId: string;
  partNo: number;
  size: number;
  state: 'open' | 'completing' | 'accepted' | 'aborted';
  pieces: Map<number, number>;
}

export type PutOutcome = number | 'network';

/** A deterministic, memory-free Blob stand-in: only `size`, `slice` and `arrayBuffer` are needed. */
export class SyntheticBlob {
  constructor(readonly size: number, private readonly offset = 0, readonly name = 'clip.mp4', readonly lastModified = 1) {}

  slice(start = 0, end = this.size): SyntheticBlob {
    const from = Math.max(0, Math.min(start, this.size));
    const to = Math.max(from, Math.min(end, this.size));
    return new SyntheticBlob(to - from, this.offset + from, this.name, this.lastModified);
  }

  async arrayBuffer(): Promise<ArrayBuffer> {
    const bytes = new Uint8Array(this.size);
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = ((this.offset + i) * 31 + 7) & 255;
    return bytes.buffer;
  }
}

export function asFile(size: number, seed = 0): Blob & { name: string; lastModified: number } {
  return new SyntheticBlob(size, seed) as unknown as Blob & { name: string; lastModified: number };
}

function problem(status: number, code: string, detail = '', headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title: code, status, code, detail, request_id: 'r' }), {
    status, headers: { 'Content-Type': 'application/problem+json', ...headers },
  });
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

export class FakeBackend {
  readonly token = 'tok_0123456789abcdefghijklmnop';
  readonly sessionId = '11111111-1111-4111-8111-111111111111';
  readonly calls: RecordedCall[] = [];
  readonly uploads = new Map<string, UploadRow>();
  captureMode: 'guided' | 'single_video' = 'guided';
  source: VideoUploadResponse | null = null;
  parts: PartSummary[];
  sessionState: 'capturing' | 'finalized' = 'capturing';
  nowMs = Date.now();
  urlTtlMs = 15 * 60_000;
  urlRefreshLimit = 30;
  /** Statuses returned for upcoming PUTs of a piece, consumed in order; absent means 200. */
  putScript = new Map<number, PutOutcome[]>();
  /** Scripted answers for the next URL-issuing requests (consumed first). */
  urlsScript: Array<{ status: number; retryAfter?: string }> = [];
  /** Next complete() calls: drop these pieces from storage and answer 409 UPLOAD_INCOMPLETE. */
  completeIncomplete: Array<{ drop: number[] }> = [];
  completeStatuses: number[] = [];
  /** When true, storage PUTs never finish unless aborted. */
  holdPuts = false;
  /** When set, every request answers 401 (expired / lost session). */
  unauthorized = false;
  consentVersion = 'upclose-prototype-v1';
  /** Free Part-1 verdict served by the progress route (null until a test sets one). */
  freeVerdict: Part1Verdict | null = null;
  /** Report identity/state served by the progress route; set by finalize to `building`. */
  report: ReportProgress | null = null;
  /** Scripted answers for the next progress reads (consumed first), e.g. 429 with Retry-After. */
  progressScript: Array<{ status: number; retryAfter?: string }> = [];
  /** Sources served by GET /sessions/{id}/sources; `sourcesStatus` 404 mimics an API without the route. */
  sources: CaptureSource[] = [];
  sourcesStatus: 200 | 404 = 200;
  checkoutUrl = 'https://checkout.test/c/cs_test_123';
  private sequence = 0;
  private readonly issued = new Map<string, { uploadId: string; part: number; expiresAt: number; revoked: boolean }>();
  readonly urlBatches: number[][] = [];
  private urlRequests = 0;

  constructor() {
    this.parts = ZONES.map((zone, index) => ({
      id: `00000000-0000-4000-8000-00000000000${index + 1}`, part_no: index + 1, zone, state: 'pending', reason: null, active_upload_id: null,
    }));
  }

  /** Make every URL issued so far fail with 403, as after expiry. */
  expireIssuedUrls(): void {
    for (const entry of this.issued.values()) entry.revoked = true;
  }

  putCount(uploadId: string, piece: number): number {
    return this.calls.filter((call) => call.method === 'PUT' && call.url.startsWith(`https://storage.test/${uploadId}/${piece}?`)).length;
  }

  callsTo(method: string, pathPart: string): RecordedCall[] {
    return this.calls.filter((call) => call.method === method && call.url.includes(pathPart));
  }

  readonly fetch = async (input: string, init: RequestInit = {}): Promise<Response> => {
    const method = (init.method ?? 'GET').toUpperCase();
    const headers = Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>));
    let body: unknown;
    if (typeof init.body === 'string') body = JSON.parse(init.body);
    else if (init.body !== undefined && init.body !== null) body = { size: (init.body as { size: number }).size };
    this.calls.push({ method, url: input, body, headers });
    if (input.startsWith('https://storage.test/')) return this.put(input, init);
    return this.api(method, input.replace(/^\/api\/v1/, ''), body, headers);
  };

  private put(url: string, init: RequestInit): Response | Promise<Response> {
    if (init.signal?.aborted) throw new DOMException('aborted', 'AbortError');
    if (this.holdPuts) {
      // Simulates a slow in-flight PUT that only ends when the client aborts it.
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
      });
    }
    const entry = this.issued.get(url);
    const match = /\/([^/]+)\/(\d+)\?/.exec(url);
    if (!entry || !match) return new Response('', { status: 404 });
    const piece = Number(match[2]);
    const scripted = this.putScript.get(piece)?.shift();
    if (scripted === 'network') throw new TypeError('network down');
    if (typeof scripted === 'number') return new Response('', { status: scripted });
    if (entry.revoked || entry.expiresAt <= this.nowMs) return new Response('<Error>expired</Error>', { status: 403 });
    const upload = this.uploads.get(entry.uploadId);
    if (!upload || upload.state === 'aborted') return new Response('', { status: 404 });
    upload.pieces.set(piece, (init.body as { size: number }).size);
    return new Response('', { status: 200, headers: { ETag: '"etag"' } });
  }

  private api(method: string, path: string, body: unknown, headers: Record<string, string>): Response {
    if (method === 'POST' && path === '/sessions') return this.createSession(body);
    if (this.unauthorized || headers.Authorization !== `Bearer ${this.token}`) return problem(401, 'TOKEN_INVALID');
    let m: RegExpExecArray | null;
    if ((m = /^\/sessions\/([^/]+)$/.exec(path)) && method === 'GET') return this.getSession(m[1]);
    if ((m = /^\/sessions\/([^/]+)\/delete-media$/.exec(path)) && method === 'POST') return json(202, { state: 'deleting' });
    if (/^\/sessions\/([^/]+)\/sources$/.test(path) && method === 'GET') return this.sourcesStatus === 404 ? problem(404, 'NOT_FOUND') : json(200, { sources: this.sources });
    if ((m = /^\/sessions\/([^/]+)\/progress$/.exec(path)) && method === 'GET') return this.getProgress(m[1]);
    if ((m = /^\/reports\/([^/]+)\/checkout$/.exec(path)) && method === 'POST') return this.checkout(m[1], body);
    if ((m = /^\/sessions\/([^/]+)\/video$/.exec(path))) {
      if (method === 'GET') return json(200, { source: this.source?.state === 'aborted' ? null : this.source });
      if (method === 'POST') {
        if (this.source && this.source.state !== 'aborted') return problem(409, 'STATE_CONFLICT');
        const request = body as CreateVideoUploadRequest;
        const id = `source-${++this.sequence}`;
        this.source = { upload_id: id, state: 'open', processing_state: 'pending', reason: null,
          expected_bytes: request.content_length, content_type: request.content_type, duration_ms: null,
          segments: request.segments ?? [], pieces: [] };
        this.uploads.set(id, { id, partId: '', partNo: 0, size: request.content_length, state: 'open', pieces: new Map() });
        return json(201, { upload_id: id, chunk_size_bytes: CHUNK, chunk_count: Math.ceil(request.content_length / CHUNK) });
      }
    }
    if ((m = /^\/video-uploads\/([^/]+)\/urls$/.exec(path)) && method === 'POST') return this.urls(m[1], body as { part_numbers: number[] });
    if ((m = /^\/video-uploads\/([^/]+)\/complete$/.exec(path)) && method === 'POST') {
      const response = this.complete(m[1]);
      if (!response.ok || !this.source) return response;
      this.source.state = 'accepted'; this.source.processing_state = 'queued';
      return json(202, { upload_id: m[1], state: 'accepted', processing_state: 'queued' });
    }
    if ((m = /^\/video-uploads\/([^/]+)$/.exec(path))) {
      if (method === 'DELETE') {
        const response = this.abort(m[1]);
        if (response.ok && this.source) this.source.state = 'aborted';
        return response;
      }
      if (method === 'GET' && this.source) return json(200, { ...this.source, pieces: [...(this.uploads.get(m[1])?.pieces ?? [])].map(([part_number, size_bytes]) => ({ part_number, size_bytes, etag: 'etag' })) });
    }
    if ((m = /^\/sessions\/([^/]+)\/parts$/.exec(path)) && method === 'POST') return this.createUpload(body as { part_no: number; content_length: number });
    if ((m = /^\/sessions\/([^/]+)\/finalize$/.exec(path)) && method === 'POST') return this.finalize();
    if ((m = /^\/parts\/([^/]+)\/skip$/.exec(path)) && method === 'POST') return this.skip(m[1]);
    if ((m = /^\/uploads\/([^/]+)\/urls$/.exec(path)) && method === 'POST') return this.urls(m[1], body as { part_numbers: number[] });
    if ((m = /^\/uploads\/([^/]+)\/complete$/.exec(path)) && method === 'POST') return this.complete(m[1]);
    if ((m = /^\/uploads\/([^/]+)$/.exec(path))) {
      if (method === 'GET') return this.getUpload(m[1]);
      if (method === 'DELETE') return this.abort(m[1]);
    }
    return problem(404, 'NOT_FOUND');
  }

  private createSession(body: unknown): Response {
    const request = body as { consent?: unknown; consent_version?: string; capture_mode?: 'guided' | 'single_video' };
    this.captureMode = request.capture_mode ?? 'guided';
    if (request.consent !== true) return problem(422, 'VALIDATION');
    if (request.consent_version !== this.consentVersion) return problem(422, 'VALIDATION');
    return json(201, { session_id: this.sessionId, session_token: this.token });
  }

  private getSession(id: string): Response {
    if (id !== this.sessionId) return problem(404, 'NOT_FOUND');
    return json(200, {
      session_id: this.sessionId, capture_mode: this.captureMode, purpose: 'buyer', state: this.sessionState, consent_at: '2026-10-28T12:00:00Z',
      listing_text: null, listing_version: 1, parts: this.parts,
    });
  }

  private getProgress(id: string): Response {
    if (id !== this.sessionId) return problem(404, 'NOT_FOUND');
    const scripted = this.progressScript.shift();
    if (scripted) {
      return problem(scripted.status, scripted.status === 429 ? 'RATE_LIMITED' : 'INTERNAL', '',
        scripted.retryAfter ? { 'Retry-After': scripted.retryAfter } : {});
    }
    return json(200, { state: this.sessionState, free_verdict: this.freeVerdict, report: this.report, parts: this.parts });
  }

  private checkout(slug: string, body: unknown): Response {
    if ((body as { product?: string }).product !== 'report_unlock') return problem(422, 'VALIDATION');
    if (!this.report || this.report.slug !== slug) return problem(404, 'NOT_FOUND');
    if (this.report.state !== 'ready') return problem(409, 'REPORT_NOT_READY', 'The report is not ready to unlock yet.');
    return json(201, { checkout_url: this.checkoutUrl });
  }

  private createUpload(body: { part_no: number; content_length: number }): Response {
    const part = this.parts.find((candidate) => candidate.part_no === body.part_no);
    if (!part) return problem(404, 'NOT_FOUND');
    if (part.state !== 'pending') return problem(409, 'STATE_CONFLICT');
    if (body.content_length > 200 * 1024 * 1024) return problem(413, 'UPLOAD_TOO_LARGE');
    this.sequence += 1;
    const id = `22222222-2222-4222-8222-${String(this.sequence).padStart(12, '0')}`;
    this.uploads.set(id, { id, partId: part.id, partNo: part.part_no, size: body.content_length, state: 'open', pieces: new Map() });
    part.state = 'uploading';
    part.active_upload_id = id;
    return json(201, {
      part_id: part.id, upload_id: id, chunk_size_bytes: CHUNK, chunk_count: Math.ceil(body.content_length / CHUNK),
    });
  }

  private urls(uploadId: string, body: { part_numbers: number[] }): Response {
    const upload = this.uploads.get(uploadId);
    if (!upload) return problem(404, 'NOT_FOUND');
    if (body.part_numbers.length > 10) return problem(422, 'VALIDATION');
    const scripted = this.urlsScript.shift();
    if (scripted) {
      return problem(scripted.status, scripted.status === 429 ? 'RATE_LIMITED' : 'INTERNAL', '',
        scripted.retryAfter ? { 'Retry-After': scripted.retryAfter } : {});
    }
    this.urlRequests += 1;
    if (this.urlRequests > this.urlRefreshLimit) return problem(429, 'RATE_LIMITED', '', { 'Retry-After': '30' });
    this.urlBatches.push([...body.part_numbers]);
    const expiresAt = this.nowMs + this.urlTtlMs;
    const items = body.part_numbers.map((part) => {
      this.sequence += 1;
      const url = `https://storage.test/${uploadId}/${part}?sig=${this.sequence}`;
      this.issued.set(url, { uploadId, part, expiresAt, revoked: false });
      return { part_number: part, url, expires_at: new Date(expiresAt).toISOString(), headers: {} };
    });
    return json(200, { items });
  }

  private getUpload(id: string): Response {
    const upload = this.uploads.get(id);
    if (!upload) return problem(404, 'NOT_FOUND');
    const pieces = upload.state === 'open' || upload.state === 'completing'
      ? [...upload.pieces].sort(([a], [b]) => a - b).map(([part_number, size_bytes]) => ({ part_number, etag: '"etag"', size_bytes }))
      : [];
    return json(200, { upload_id: id, state: upload.state, pieces });
  }

  private complete(id: string): Response {
    const upload = this.uploads.get(id);
    if (!upload) return problem(404, 'NOT_FOUND');
    if (upload.state === 'aborted') return problem(409, 'STATE_CONFLICT');
    if (upload.state !== 'open') {
      // Repeat completion is naturally idempotent: it reports status, never a second object.
      const settled = this.parts.find((candidate) => candidate.id === upload.partId);
      return json(202, { part_id: upload.partId, state: settled?.state ?? 'uploaded' });
    }
    const status = this.completeStatuses.shift();
    if (status !== undefined) return problem(status, 'INTERNAL');
    const incomplete = this.completeIncomplete.shift();
    if (incomplete) {
      for (const piece of incomplete.drop) upload.pieces.delete(piece);
      return problem(409, 'UPLOAD_INCOMPLETE');
    }
    const count = Math.ceil(upload.size / CHUNK);
    for (let n = 1; n <= count; n += 1) {
      const expected = n < count ? CHUNK : upload.size - (count - 1) * CHUNK;
      if (upload.pieces.get(n) !== expected) return problem(409, 'UPLOAD_INCOMPLETE');
    }
    upload.state = 'accepted';
    const part = this.parts.find((candidate) => candidate.id === upload.partId);
    if (part) {
      part.state = 'uploaded';
      part.active_upload_id = null;
    }
    return json(202, { part_id: upload.partId, state: 'uploaded' });
  }

  private abort(id: string): Response {
    const upload = this.uploads.get(id);
    if (!upload) return problem(404, 'NOT_FOUND');
    if (upload.state === 'open') {
      upload.state = 'aborted';
      const part = this.parts.find((candidate) => candidate.id === upload.partId);
      if (part?.state === 'uploading') part.state = 'pending';
      if (part) part.active_upload_id = null;
    } else if (upload.state !== 'aborted') {
      return problem(409, 'STATE_CONFLICT');
    }
    return new Response(null, { status: 204 });
  }

  private skip(partId: string): Response {
    const part = this.parts.find((candidate) => candidate.id === partId);
    if (!part) return problem(404, 'NOT_FOUND');
    if (part.state !== 'pending') return problem(409, 'STATE_CONFLICT');
    part.state = 'skipped';
    return json(200, { state: 'skipped' });
  }

  private finalize(): Response {
    if (this.source && (this.source.state === 'open' || this.source.state === 'completing' || this.source.processing_state === 'queued')) return problem(409, 'STATE_CONFLICT');
    if (this.parts.some((part) => part.state === 'uploading')) return problem(409, 'STATE_CONFLICT');
    this.parts.forEach((part) => { if (part.state === 'pending') part.state = 'skipped'; });
    this.sessionState = 'finalized';
    this.report = { slug: 'slug-abc', state: 'building', unlocked: false };
    return json(202, { state: 'finalized', report_slug: 'slug-abc' });
  }
}

const CONFIDENCE_LABEL = 'Uncalibrated model confidence, not an accuracy probability' as const;

/** Mirrors what the fixture provider yields: one synthetic rust finding, score 94.0, nothing locked. */
export function syntheticVerdict(overrides: Partial<Part1Verdict> = {}): Part1Verdict {
  return {
    coverage: [{ zone: 'driver_side', state: 'assessed', reason: null }],
    part1_score: 94,
    findings: [{
      id: '33333333-3333-4333-8333-333333333333', zone: 'driver_side', concept: 'rust', severity: 2, confidence: 0.8,
      confidence_label: CONFIDENCE_LABEL, evidence_note: 'Synthetic fixture verdict; no real inference was performed.',
      subzone: 'rocker', component_id: null, flags: [], bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, evidence_available: false, source_timestamp_ms: null, mapping_basis: null,
    }],
    locked_zones: [],
    part1_summary: 'Driver side: 1 visible finding in assessed footage (synthetic).',
    ...overrides,
  };
}
