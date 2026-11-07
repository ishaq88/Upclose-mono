import type { UploadContentType } from '../upload/multipart';

const ACCEPTED: ReadonlySet<string> = new Set<UploadContentType>(['video/mp4', 'video/webm', 'video/quicktime']);

/**
 * Map a File/Blob to a content type the API accepts, or null. The server never trusts this value; it only
 * has to be plausible. MediaRecorder types carry codec parameters (`video/webm;codecs=vp9`) that are dropped.
 */
export function normalizeVideoType(file: { type: string; name?: string }): UploadContentType | null {
  const base = file.type.split(';')[0]?.trim().toLowerCase() ?? '';
  if (ACCEPTED.has(base)) return base as UploadContentType;
  if (base === '' || base === 'application/octet-stream' || base.startsWith('video/')) {
    const name = (file.name ?? '').toLowerCase();
    if (name.endsWith('.mp4') || name.endsWith('.m4v')) return 'video/mp4';
    if (name.endsWith('.mov')) return 'video/quicktime';
    if (name.endsWith('.webm')) return 'video/webm';
  }
  return null;
}

interface RecorderCtor {
  isTypeSupported?: (type: string) => boolean;
}

/** Request MP4 first; otherwise return undefined so the platform default is used. */
export function pickRecorderMimeType(ctor: RecorderCtor | undefined): string | undefined {
  if (!ctor || typeof ctor.isTypeSupported !== 'function') return undefined;
  try {
    return ctor.isTypeSupported('video/mp4') ? 'video/mp4' : undefined;
  } catch {
    return undefined;
  }
}

export type CameraSupport = 'available' | 'insecure_context' | 'unsupported';

export function detectCameraSupport(): CameraSupport {
  if (typeof window === 'undefined') return 'unsupported';
  if (!window.isSecureContext) return 'insecure_context';
  const hasCamera = typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.getUserMedia === 'function';
  return hasCamera && typeof window.MediaRecorder === 'function' ? 'available' : 'unsupported';
}

export type CameraFailure = 'denied' | 'no_camera' | 'busy' | 'other';

export function classifyCameraError(error: unknown): CameraFailure {
  const name = (error as { name?: string } | null)?.name ?? '';
  if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') return 'denied';
  if (name === 'NotFoundError' || name === 'OverconstrainedError' || name === 'DevicesNotFoundError') return 'no_camera';
  if (name === 'NotReadableError' || name === 'AbortError' || name === 'TrackStartError') return 'busy';
  return 'other';
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KiB`;
  return `${bytes} B`;
}
