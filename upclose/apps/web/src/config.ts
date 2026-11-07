/** Client configuration constants. Nothing here is secret; tokens live only in sessionStorage. */

/** Must match the backend default `consent_version` (services/api/app/settings.py). */
export const CONSENT_VERSION = 'upclose-prototype-v1';

export const API_BASE = '/api/v1';

/** Server limits (implementation contract section 3); the server remains the authority. */
export const MAX_CLIP_BYTES = 200 * 1024 * 1024;
export const MAX_LISTING_CHARS = 10_000;
export const MAX_URLS_PER_REQUEST = 10;

/** Recording cap; matches the 60 s ingest design default (contract section 5). */
export const MAX_RECORDING_SECONDS = 60;

/** How often owner session + progress state is re-read from the server (one coalesced poll). */
export const SESSION_POLL_MS = 3_000;

/** How often attached capture-kit sources are re-read while the capture screen is open. */
export const SOURCES_POLL_MS = 5_000;

/** Ceiling for exponential back-off after 429/5xx/network failures, and for a server Retry-After. */
export const POLL_MAX_BACKOFF_MS = 30_000;
export const POLL_MAX_RETRY_AFTER_MS = 120_000;
