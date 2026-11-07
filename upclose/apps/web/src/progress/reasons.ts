/**
 * Plain-language copy for the server's part reason codes (`PartSummary.reason`). The codes are
 * non-secret enum-like strings; unknown ones get generic text. A failed or unassessed zone is never
 * described as clean.
 */
const NOT_ASSESSED = 'This zone was not assessed, which is not a clean result.';

const KNOWN: Record<string, string> = {
  INGEST_JOB_DEAD: `Processing of this clip stopped after repeated attempts. ${NOT_ASSESSED}`,
  ASSESSMENT_INPUT_MISSING: `The inputs for this zone's assessment were missing. ${NOT_ASSESSED}`,
  INGEST_CONFIG_MISMATCH: `Processing settings changed while this clip was being read. ${NOT_ASSESSED}`,
  INGEST_OBJECT_MISSING: `The uploaded video could not be found in storage. ${NOT_ASSESSED}`,
  INGEST_OBJECT_CHANGED: `The stored video changed after it was uploaded, so it was rejected. ${NOT_ASSESSED}`,
  INGEST_STORAGE_UNAVAILABLE: `Storage was unavailable while this clip was processed. ${NOT_ASSESSED}`,
  INGEST_TIMEOUT: `Processing this clip took too long and was stopped. ${NOT_ASSESSED}`,
  INGEST_INTERNAL_ERROR: `An internal error stopped processing of this clip. ${NOT_ASSESSED}`,
  UPLOAD_SIZE_MISMATCH: 'The stored video did not match its declared size, so the upload was discarded. Failed and skipped zones cannot be redone in this session.',
  UPLOAD_LOST: 'The upload could not be found in storage.',
};

const MEDIA: Record<string, string> = {
  INVALID_INPUT: 'The file could not be read as a video.',
  CORRUPT: 'The video file appears to be damaged.',
  NO_VIDEO: 'The file has no video track.',
  MULTIPLE_VIDEO_STREAMS: 'The file has more than one video track, which is not supported.',
  UNSUPPORTED_CONTAINER: 'The video file format is not supported.',
  UNSUPPORTED_CODEC: 'The video encoding is not supported.',
  UNSUPPORTED_ROTATION: 'The video orientation is not supported.',
  DURATION_EXCEEDED: 'The video is longer than the maximum clip length.',
  DIMENSIONS_EXCEEDED: 'The video resolution is larger than supported.',
  RESOURCE_LIMIT: 'The video was too demanding to process within limits.',
  TIMEOUT: 'Reading the video took too long.',
  TRANSCODE_FAILED: 'The video could not be converted for analysis.',
  OUTPUT_INVALID: 'The converted video was not usable.',
  EXTRACT_FAILED: 'Frames could not be extracted from the video.',
};

const SAFE_CODE = /^[A-Z][A-Z0-9_]{0,59}$/;

export function describeReason(code: string | null | undefined): string {
  if (!code) return `This zone failed with no reason given. ${NOT_ASSESSED}`;
  const known = KNOWN[code];
  if (known) return known;
  if (code.startsWith('MEDIA_')) {
    const detail = MEDIA[code.slice('MEDIA_'.length)] ?? 'The video could not be processed.';
    return `${detail} ${NOT_ASSESSED}`;
  }
  const shown = SAFE_CODE.test(code) ? ` (code ${code})` : '';
  return `This zone could not be processed${shown}. ${NOT_ASSESSED}`;
}

/**
 * True when the reason code says the footage itself was too blurry, dark or shaky to assess.
 * The backend has no dedicated quality code yet, so this matches on the code text; unknown codes never match.
 */
export function isQualityReason(code: string | null | undefined): boolean {
  return !!code && /BLUR|QUALITY|TOO_DARK|SHAKY/.test(code);
}
