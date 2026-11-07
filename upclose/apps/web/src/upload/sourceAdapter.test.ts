import { expect, it } from 'vitest';
import { createApi } from '../api/client';
import { FakeBackend, asFile } from '../testing/fakeBackend';
import { resumeUpload, startUpload, type UploadRecord } from './multipart';
import { sourceUploadEnv } from './sourceAdapter';

it('source adapter resumes missing pieces and never creates a guided part', async () => {
  const backend = new FakeBackend();
  const api = createApi({ getToken: () => backend.token, fetchImpl: backend.fetch });
  const env = { ...sourceUploadEnv(api, [{ part_no: 2, start_ms: 1000, end_ms: 4000 }]), fetchImpl: backend.fetch, sleep: async () => {} };
  const file = asFile(8 * 1024 * 1024 + 12);
  let record: UploadRecord | undefined;
  backend.putScript.set(2, [500]);
  await expect(startUpload(env, { sessionId: backend.sessionId, partNo: 0, file, contentType: 'video/mp4', idempotencyKey: 'source-attempt', onRecord: (value) => { record = value; } }, { concurrency: 1, maxPieceAttempts: 1 })).rejects.toThrow();
  expect(record).toBeDefined();
  expect(backend.putCount(record!.uploadId, 1)).toBe(1);
  const accepted = await resumeUpload(env, record!, file);
  expect(accepted.processing_state).toBe('queued');
  expect(backend.putCount(record!.uploadId, 1)).toBe(1);
  expect(backend.putCount(record!.uploadId, 2)).toBe(2);
  expect(backend.callsTo('POST', '/parts')).toHaveLength(0);
  expect(backend.source?.segments).toEqual([{ part_no: 2, start_ms: 1000, end_ms: 4000 }]);
  for (const call of backend.calls.filter((item) => item.method === 'PUT')) expect(call.headers.Authorization).toBeUndefined();
});
