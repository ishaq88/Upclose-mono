import type { Api, VideoAcceptedResponse, VideoRange } from '../api/client';
import { abortableSleep, type UploadEnv } from './multipart';

/** Only endpoint selection changes; the tested piece/retry/resume protocol is shared. */
export function sourceUploadEnv(api: Api, segments: VideoRange[]): UploadEnv<VideoAcceptedResponse> {
  return {
    api: {
      createUpload: (id, request, key) => api.createVideoUpload(id, {
        content_type: request.content_type, content_length: request.content_length, segments,
      }, key),
      uploadUrls: api.videoUploadUrls,
      getUpload: api.getVideoUpload,
      completeUpload: api.completeVideoUpload,
      abortUpload: api.abortVideoUpload,
    },
    fetchImpl: (input, init) => globalThis.fetch(input, init),
    sleep: abortableSleep, now: Date.now, random: Math.random,
  };
}
