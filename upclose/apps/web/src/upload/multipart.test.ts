import { describe, expect, it, vi } from 'vitest';
import { createApi } from '../api/client';
import { FakeBackend, asFile } from '../testing/fakeBackend';
import {
  abortAttempt, abortableSleep, fingerprintBlob, isUploadAbort, missingPieces, pieceRange, pieceSize, resumeUpload,
  startUpload, UploadError, type UploadEnv, type UploadRecord,
} from './multipart';

const MIB = 1024 * 1024;
const CHUNK = 8 * MIB;

function setup(backend = new FakeBackend()) {
  const sleeps: number[] = [];
  const env: UploadEnv = {
    api: createApi({ getToken: () => backend.token, fetchImpl: backend.fetch }),
    fetchImpl: backend.fetch,
    sleep: vi.fn(async (ms: number) => { sleeps.push(ms); backend.nowMs += ms; }),
    now: () => backend.nowMs,
    random: () => 0.5,
  };
  return { backend, env, sleeps };
}

const input = (file: Blob, partNo = 1) => ({
  sessionId: new FakeBackend().sessionId, partNo, file, contentType: 'video/mp4' as const, idempotencyKey: 'key-1',
});

async function captureRecord(env: UploadEnv, backend: FakeBackend, size: number): Promise<{ record: UploadRecord; file: Blob }> {
  // Start an upload but fail every PUT so the attempt stays open with nothing (or something) stored.
  const file = asFile(size);
  let record: UploadRecord | undefined;
  backend.putScript.set(1, ['network', 'network', 'network', 'network', 'network']);
  await expect(startUpload(env, { ...input(file), onRecord: (r) => { record = r; } }, { concurrency: 1, maxPieceAttempts: 1 }))
    .rejects.toBeInstanceOf(UploadError);
  if (!record) throw new Error('record missing');
  return { record, file };
}

describe('piece layout', () => {
  it('slices exactly chunk_size_bytes pieces with a short final piece', () => {
    const total = 2 * CHUNK + 123;
    expect(pieceRange(total, CHUNK, 1)).toEqual({ start: 0, end: CHUNK });
    expect(pieceRange(total, CHUNK, 2)).toEqual({ start: CHUNK, end: 2 * CHUNK });
    expect(pieceRange(total, CHUNK, 3)).toEqual({ start: 2 * CHUNK, end: total });
    expect(pieceSize(total, CHUNK, 3)).toBe(123);
    expect(pieceSize(2 * CHUNK, CHUNK, 2)).toBe(CHUNK);
  });

  it('computes missing pieces by number and exact size', () => {
    const record = { size: 2 * CHUNK + 5, chunkSize: CHUNK, chunkCount: 3 };
    const listed = [{ part_number: 1, etag: 'a', size_bytes: CHUNK }, { part_number: 2, etag: 'b', size_bytes: 7 }];
    expect(missingPieces(record, listed)).toEqual([2, 3]);
  });
});

describe('startUpload', () => {
  it('PUTs every piece at its exact size and completes', async () => {
    const { backend, env } = setup();
    const size = 2 * CHUNK + 123;
    const accepted = await startUpload(env, input(asFile(size)));
    expect(accepted.state).toBe('uploaded');
    const [uploadId] = [...backend.uploads.keys()];
    const sizes = [1, 2, 3].map((n) => backend.uploads.get(uploadId)!.pieces.get(n));
    expect(sizes).toEqual([CHUNK, CHUNK, 123]);
    // The client must not set Content-Length; it never passes any header at all here.
    const puts = backend.calls.filter((call) => call.method === 'PUT');
    expect(puts).toHaveLength(3);
    for (const put of puts) expect(Object.keys(put.headers).map((h) => h.toLowerCase())).not.toContain('content-length');
    expect(backend.callsTo('POST', '/parts')[0]?.headers['Idempotency-Key']).toBe('key-1');
    expect(backend.callsTo('POST', '/parts')[0]?.body).toEqual({ part_no: 1, content_type: 'video/mp4', content_length: size });
  });

  it('requests at most 10 URLs per call and covers every piece', async () => {
    const { backend, env } = setup();
    const size = 24 * CHUNK + 1; // 25 pieces
    await startUpload(env, input(asFile(size)), { concurrency: 2 });
    expect(backend.urlBatches.length).toBeGreaterThanOrEqual(3);
    for (const batch of backend.urlBatches) expect(batch.length).toBeLessThanOrEqual(10);
    expect(new Set(backend.urlBatches.flat())).toEqual(new Set(Array.from({ length: 25 }, (_, i) => i + 1)));
  });

  it('retries only the failed piece with backoff and leaves other pieces untouched', async () => {
    const { backend, env, sleeps } = setup();
    backend.putScript.set(2, [500, 'network']);
    await startUpload(env, input(asFile(3 * CHUNK)), { concurrency: 1 });
    const [uploadId] = [...backend.uploads.keys()];
    expect(backend.putCount(uploadId, 1)).toBe(1);
    expect(backend.putCount(uploadId, 2)).toBe(3);
    expect(backend.putCount(uploadId, 3)).toBe(1);
    expect(sleeps).toEqual([375, 750]); // 500*0.75, 1000*0.75: exponential, jittered
  });

  it('gives up on a piece after the attempt budget and reports a piece failure', async () => {
    const { backend, env } = setup();
    backend.putScript.set(1, [500, 500, 500]);
    await expect(startUpload(env, input(asFile(CHUNK)), { maxPieceAttempts: 3 })).rejects.toMatchObject({ kind: 'piece' });
    expect(backend.callsTo('POST', '/complete')).toHaveLength(0);
  });

  it('refreshes expired URLs after a 403 for the same upload', async () => {
    const { backend, env } = setup();
    backend.putScript.set(1, [403]);
    await startUpload(env, input(asFile(CHUNK)));
    expect(backend.urlBatches).toHaveLength(2);
    const [uploadId] = [...backend.uploads.keys()];
    expect(backend.putCount(uploadId, 1)).toBe(2);
  });

  it('refreshes proactively when a cached URL is about to expire', async () => {
    const { backend, env } = setup();
    backend.urlTtlMs = 20_000;
    backend.putScript.set(1, ['network']);
    env.sleep = vi.fn(async (ms: number) => { backend.nowMs += ms + 10_000; });
    await startUpload(env, input(asFile(CHUNK)), { urlSkewMs: 15_000 });
    expect(backend.urlBatches.length).toBeGreaterThanOrEqual(2);
  });

  it('bounds repeated 403s instead of looping forever', async () => {
    const { backend, env } = setup();
    backend.putScript.set(1, [403, 403, 403, 403, 403, 403]);
    await expect(startUpload(env, input(asFile(CHUNK)), { maxUrlRefreshesPerPiece: 2 })).rejects.toMatchObject({ kind: 'piece' });
  });

  it('waits Retry-After on a 429 from the URL endpoint, then continues', async () => {
    const { backend, env, sleeps } = setup();
    backend.urlsScript.push({ status: 429, retryAfter: '7' });
    await startUpload(env, input(asFile(CHUNK)));
    expect(sleeps).toContain(7000);
    expect(backend.urlBatches).toHaveLength(1);
  });

  it('surfaces a rate-limit failure when Retry-After exceeds the cap', async () => {
    const { backend, env } = setup();
    backend.urlsScript.push({ status: 429, retryAfter: '600' });
    await expect(startUpload(env, input(asFile(CHUNK)))).rejects.toMatchObject({ kind: 'rate_limited', retryAfterSeconds: 600 });
  });

  it('recovers from 409 UPLOAD_INCOMPLETE by re-listing and re-sending only missing pieces', async () => {
    const { backend, env } = setup();
    backend.completeIncomplete.push({ drop: [2] });
    const accepted = await startUpload(env, input(asFile(3 * CHUNK)), { concurrency: 1 });
    expect(accepted.state).toBe('uploaded');
    const [uploadId] = [...backend.uploads.keys()];
    expect(backend.putCount(uploadId, 1)).toBe(1);
    expect(backend.putCount(uploadId, 2)).toBe(2);
    expect(backend.putCount(uploadId, 3)).toBe(1);
    expect(backend.callsTo('POST', '/complete')).toHaveLength(2);
  });

  it('retries completion on a transient 503 without re-uploading', async () => {
    const { backend, env } = setup();
    backend.completeStatuses.push(503);
    await startUpload(env, input(asFile(CHUNK)));
    const [uploadId] = [...backend.uploads.keys()];
    expect(backend.putCount(uploadId, 1)).toBe(1);
    expect(backend.callsTo('POST', '/complete')).toHaveLength(2);
  });

  it('does not treat a STATE_CONFLICT on completion as recoverable', async () => {
    const { backend, env } = setup();
    const original = backend.fetch;
    const failing = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/complete')) return new Response(JSON.stringify({ code: 'STATE_CONFLICT', detail: '' }), { status: 409 });
      return original(url, init);
    });
    env.api = createApi({ getToken: () => backend.token, fetchImpl: failing });
    await expect(startUpload(env, input(asFile(CHUNK)))).rejects.toMatchObject({ kind: 'gone' });
  });

  it('passes the user-visible progress of accepted pieces', async () => {
    const { env } = setup();
    const progress: number[] = [];
    await startUpload(env, input(asFile(2 * CHUNK)), { concurrency: 1, onProgress: (p) => { if (p.phase === 'uploading') progress.push(p.donePieces); } });
    expect(progress).toEqual([1, 2]);
  });
});

describe('abort on retake', () => {
  it('stops in-flight PUTs when the signal aborts and releases the server attempt', async () => {
    const { backend, env } = setup();
    backend.holdPuts = true;
    const controller = new AbortController();
    const pending = startUpload(env, { ...input(asFile(2 * CHUNK)), onRecord: () => undefined }, { signal: controller.signal });
    await vi.waitFor(() => expect(backend.calls.some((call) => call.method === 'PUT')).toBe(true));
    controller.abort();
    const error = await pending.catch((e: unknown) => e);
    expect(isUploadAbort(error)).toBe(true);
    const [uploadId] = [...backend.uploads.keys()];
    await abortAttempt(env, uploadId);
    expect(backend.uploads.get(uploadId)?.state).toBe('aborted');
    expect(backend.parts[0].state).toBe('pending');
    expect(backend.callsTo('POST', '/complete')).toHaveLength(0);
    // Aborting again is idempotent.
    await expect(abortAttempt(env, uploadId)).resolves.toBeUndefined();
  });

  it('a retake can start a fresh attempt after the abort', async () => {
    const { backend, env } = setup();
    const { record } = await captureRecord(env, backend, CHUNK);
    await abortAttempt(env, record.uploadId);
    const accepted = await startUpload(env, { ...input(asFile(CHUNK)), idempotencyKey: 'key-2' });
    expect(accepted.state).toBe('uploaded');
    expect(backend.uploads.size).toBe(2);
  });

  it('abortableSleep rejects with an abort error', async () => {
    const controller = new AbortController();
    const sleeping = abortableSleep(60_000, controller.signal);
    controller.abort();
    await expect(sleeping).rejects.toMatchObject({ kind: 'aborted' });
  });
});

describe('resumeUpload', () => {
  it('skips pieces storage already holds and sends only the rest', async () => {
    const { backend, env } = setup();
    // Piece 1 uploads, then piece 2 keeps failing -> attempt stays open with piece 1 stored.
    const file = asFile(3 * CHUNK + 9);
    let record: UploadRecord | undefined;
    backend.putScript.set(2, Array(5).fill(500));
    await expect(startUpload(env, { ...input(file), onRecord: (r) => { record = r; } }, { concurrency: 1, maxPieceAttempts: 2 }))
      .rejects.toBeInstanceOf(UploadError);
    const uploadId = record!.uploadId;
    expect([...backend.uploads.get(uploadId)!.pieces.keys()]).toEqual([1]);
    const before = backend.putCount(uploadId, 1);

    const accepted = await resumeUpload(env, record!, file, { concurrency: 1 });
    expect(accepted.state).toBe('uploaded');
    expect(backend.putCount(uploadId, 1)).toBe(before); // not re-sent
    expect(backend.putCount(uploadId, 3)).toBe(1);
    expect(backend.putCount(uploadId, 4)).toBe(1);
  });

  it('rejects a file of a different size without touching storage', async () => {
    const { backend, env } = setup();
    const { record } = await captureRecord(env, backend, 2 * CHUNK);
    const putsBefore = backend.calls.filter((c) => c.method === 'PUT').length;
    await expect(resumeUpload(env, record, asFile(2 * CHUNK + 1))).rejects.toMatchObject({ kind: 'wrong_file' });
    expect(backend.calls.filter((c) => c.method === 'PUT')).toHaveLength(putsBefore);
  });

  it('rejects a same-size file whose contents differ (fingerprint)', async () => {
    const { backend, env } = setup();
    const { record } = await captureRecord(env, backend, 2 * CHUNK);
    await expect(resumeUpload(env, record, asFile(2 * CHUNK, 12345))).rejects.toMatchObject({ kind: 'wrong_file' });
  });

  it('rejects when listed pieces have sizes that cannot belong to this file', async () => {
    const { backend, env } = setup();
    const { record, file } = await captureRecord(env, backend, 2 * CHUNK);
    backend.uploads.get(record.uploadId)!.pieces.set(1, 999);
    await expect(resumeUpload(env, record, file)).rejects.toMatchObject({ kind: 'wrong_file' });
  });

  it('reports an aborted attempt as gone so the part can be restarted', async () => {
    const { backend, env } = setup();
    const { record, file } = await captureRecord(env, backend, CHUNK);
    await abortAttempt(env, record.uploadId);
    await expect(resumeUpload(env, record, file)).rejects.toMatchObject({ kind: 'gone' });
  });

  it('does not re-send anything when the attempt is already accepted', async () => {
    const { backend, env } = setup();
    const file = asFile(CHUNK);
    let record: UploadRecord | undefined;
    await startUpload(env, { ...input(file), onRecord: (r) => { record = r; } });
    const puts = backend.calls.filter((c) => c.method === 'PUT').length;
    // Server state is `accepted`: the listing is empty and storage must not be written again.
    const again = await resumeUpload(env, record!, file);
    expect(again.state).toBe('uploaded');
    expect(backend.calls.filter((c) => c.method === 'PUT')).toHaveLength(puts);
  });
});

describe('fingerprintBlob', () => {
  it('is stable for equal content and differs when sampled content differs', async () => {
    expect(await fingerprintBlob(asFile(300_000))).toBe(await fingerprintBlob(asFile(300_000)));
    expect(await fingerprintBlob(asFile(300_000))).not.toBe(await fingerprintBlob(asFile(300_000, 1)));
    expect(await fingerprintBlob(asFile(300_000))).not.toBe(await fingerprintBlob(asFile(300_001)));
  });
});
