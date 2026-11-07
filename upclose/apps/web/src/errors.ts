import { ApiError } from './api/client';
import { MAX_CLIP_BYTES } from './config';
import { UploadError } from './upload/multipart';

/** What the user is told, and what (if anything) they can do about it. */
export interface UiError {
  title: string;
  message: string;
  /** `new_session` is the only recovery from a lost/expired owner token. */
  action?: 'new_session' | 'retry';
}

const MIB = 1024 * 1024;

export function describeError(error: unknown): UiError {
  if (error instanceof UploadError) return describeUploadError(error);
  if (error instanceof ApiError) return describeApiError(error);
  return { title: 'Something went wrong', message: 'An unexpected problem occurred. Reload this tab and try again.', action: 'retry' };
}

function describeUploadError(error: UploadError): UiError {
  switch (error.kind) {
    case 'wrong_file':
      return { title: 'That is not the same video', message: `${error.message} Select the exact file you started uploading, or choose "Start this zone over" to record again.` };
    case 'rate_limited':
      return { title: 'Slow down for a moment', message: `Too many upload address requests. Wait ${error.retryAfterSeconds ?? 30} seconds, then try again.`, action: 'retry' };
    case 'gone':
      return { title: 'This upload is no longer open', message: 'The upload attempt was closed or replaced. Start this zone over to record or choose the video again.' };
    case 'piece':
      return { title: 'The upload was interrupted', message: 'Part of the video could not be sent. Your connection may have dropped. Parts already sent are kept; try again to send only what is missing.', action: 'retry' };
    case 'aborted':
      return { title: 'Upload cancelled', message: 'The upload was cancelled.' };
  }
}

export function describeApiError(error: ApiError): UiError {
  if (error.status === 401 || error.code === 'TOKEN_INVALID') {
    return {
      title: 'Your session has expired or was lost',
      message: 'This browser tab no longer has valid access to its session. Sessions are not recoverable from another tab or device, and closing the tab can lose it. Start a new session to continue; videos from the old session cannot be reattached.',
      action: 'new_session',
    };
  }
  if (error.status === 404) {
    return { title: 'Not found', message: 'That item was not found in your session. It may have been removed, or it belongs to a different session. Refresh the page to see the current state.' };
  }
  if (error.status === 409) {
    if (error.code === 'UPLOAD_INCOMPLETE') {
      return { title: 'The upload did not finish cleanly', message: 'Storage does not hold every piece of the video yet. Try again; only missing pieces are re-sent.', action: 'retry' };
    }
    if (error.code === 'REPORT_NOT_READY') {
      return { title: 'The report is not ready yet', message: 'Unlocking is only available once the report is ready. This page keeps checking; try again when it shows as ready.' };
    }
    return { title: 'That step has already changed', message: 'This zone or session is in a different state than this page expected (for example it is already uploading, skipped, or the session is finished). The page has been refreshed; check the zone status and continue from there.' };
  }
  if (error.status === 413 || error.code === 'UPLOAD_TOO_LARGE') {
    return { title: 'That video is too large', message: `Each clip can be at most ${MAX_CLIP_BYTES / MIB} MiB and a session has a total limit. Record a shorter clip or use a lower resolution setting, then try again.` };
  }
  if (error.status === 429 || error.code === 'RATE_LIMITED') {
    const wait = error.retryAfterSeconds === null ? 'a little while' : `${error.retryAfterSeconds} seconds`;
    return { title: 'Too many requests', message: `The service is limiting requests from this session or network. Wait ${wait} and try again.`, action: 'retry' };
  }
  if (error.status === 422 || error.code === 'VALIDATION') {
    return { title: 'That request was not accepted', message: 'The service could not accept that input. Check it and try again.' };
  }
  if (error.status === 0 || error.code === 'NETWORK') {
    return { title: 'Cannot reach the service', message: 'Check your connection. Nothing you have already uploaded is lost; try again when you are back online.', action: 'retry' };
  }
  return { title: 'The service had a problem', message: 'The service could not complete that request. Try again in a moment.', action: 'retry' };
}
