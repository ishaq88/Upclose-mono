import { ApiError } from '../api/client';

export const MIN_PASSWORD = 10;

/** Plain-language message for a failed account call. Never echoes server detail or credentials. */
export function accountErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'NETWORK') return "We couldn't reach the server. Check your connection and try again.";
    if (error.code === 'RATE_LIMITED' || error.status === 429) {
      const wait = error.retryAfterSeconds;
      return wait ? `Too many attempts. Try again in ${wait} second${wait === 1 ? '' : 's'}.` : 'Too many attempts. Wait a moment and try again.';
    }
    if (error.code === 'INVALID_CREDENTIALS') return "That email and password don't match.";
    if (error.code === 'EMAIL_TAKEN') return 'That email already has an account. Try signing in.';
    if (error.code === 'TOKEN_INVALID') return 'That link is invalid or has expired.';
    if (error.code === 'VALIDATION') return `Check the details and try again. Passwords need at least ${MIN_PASSWORD} characters.`;
    if (error.code === 'NOT_FOUND') return "We couldn't find that inspection.";
    if (error.code === 'STATE_CONFLICT') return "That inspection can't be saved to an account right now.";
  }
  return 'Something went wrong. Try again.';
}

export function passwordProblem(password: string): string | null {
  return password.length < MIN_PASSWORD ? `Use at least ${MIN_PASSWORD} characters.` : null;
}
