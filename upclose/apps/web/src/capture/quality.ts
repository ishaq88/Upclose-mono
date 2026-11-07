/**
 * Lightweight client-side quality hints. They only ever warn: nothing here blocks an upload. Thresholds are
 * the provisional PRD 7.8.2 design values (duration 8-60 s, at least 1280x720). Blur and exposure are measured
 * authoritatively on the server; the client does not compute them.
 */
export interface VideoMetadata {
  durationSeconds: number | null;
  width: number | null;
  height: number | null;
}

export interface QualityHint {
  id: 'too_short' | 'too_long' | 'low_resolution' | 'metadata_unavailable';
  severity: 'warn' | 'info';
  message: string;
}

export const MIN_SECONDS = 8;
export const MAX_SECONDS = 60;
const MIN_LONG_EDGE = 1280;
const MIN_SHORT_EDGE = 720;

export function qualityHints(meta: VideoMetadata, fallbackDurationSeconds: number | null = null): QualityHint[] {
  const hints: QualityHint[] = [];
  const duration = meta.durationSeconds ?? fallbackDurationSeconds;
  if (duration === null && meta.width === null) {
    return [{
      id: 'metadata_unavailable', severity: 'info',
      message: 'This browser could not read the video details. You can still upload it; quality is checked again after upload.',
    }];
  }
  if (duration !== null && duration < MIN_SECONDS) {
    hints.push({
      id: 'too_short', severity: 'warn',
      message: `This clip is about ${Math.round(duration)} seconds. Under ${MIN_SECONDS} seconds can miss parts of the zone. You can keep it or retake it.`,
    });
  }
  if (duration !== null && duration > MAX_SECONDS) {
    hints.push({
      id: 'too_long', severity: 'warn',
      message: `This clip is about ${Math.round(duration)} seconds. Clips over ${MAX_SECONDS} seconds may not be fully analysed. Consider a shorter pass.`,
    });
  }
  if (meta.width !== null && meta.height !== null) {
    const long = Math.max(meta.width, meta.height);
    const short = Math.min(meta.width, meta.height);
    if (long < MIN_LONG_EDGE || short < MIN_SHORT_EDGE) {
      hints.push({
        id: 'low_resolution', severity: 'warn',
        message: `The video is ${meta.width}x${meta.height}. Below 1280x720, small damage may be missed. Move closer or use a higher camera setting.`,
      });
    }
  }
  return hints;
}

/** Read duration and dimensions with a throwaway <video>. Resolves with nulls on any failure or timeout. */
export function readVideoMetadata(blob: Blob, timeoutMs = 5000): Promise<VideoMetadata> {
  const unknown: VideoMetadata = { durationSeconds: null, width: null, height: null };
  if (typeof document === 'undefined' || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
    return Promise.resolve(unknown);
  }
  return new Promise((resolve) => {
    const video = document.createElement('video');
    const url = URL.createObjectURL(blob);
    let settled = false;
    const finish = (value: VideoMetadata): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      video.removeAttribute('src');
      video.load();
      URL.revokeObjectURL(url);
      resolve(value);
    };
    const timer = setTimeout(() => finish(unknown), timeoutMs);
    video.preload = 'metadata';
    video.muted = true;
    video.onloadedmetadata = () => finish({
      // MediaRecorder WebM blobs often report Infinity until fully scanned.
      durationSeconds: Number.isFinite(video.duration) ? video.duration : null,
      width: video.videoWidth || null,
      height: video.videoHeight || null,
    });
    video.onerror = () => finish(unknown);
    video.src = url;
  });
}
