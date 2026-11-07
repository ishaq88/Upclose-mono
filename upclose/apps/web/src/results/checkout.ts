/** Only an https URL (or http on localhost, for local development) is ever navigated to. */
export function isSafeCheckoutUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' || (url.protocol === 'http:' && (url.hostname === 'localhost' || url.hostname === '127.0.0.1'));
  } catch {
    return false;
  }
}

export function defaultNavigate(url: string): void {
  window.location.assign(url);
}

const PENDING_KEY = 'upclose.checkout.pending.v1';

/**
 * Remembers which report the user sent to checkout so the return page can route back to it.
 * The slug is already in the report address; it is not a token and grants nothing.
 */
export function rememberCheckout(slug: string): void {
  try { sessionStorage.setItem(PENDING_KEY, slug); } catch { /* storage may be blocked; the return page falls back to home */ }
}

export function pendingCheckout(): string | null {
  try { return sessionStorage.getItem(PENDING_KEY); } catch { return null; }
}

export function clearPendingCheckout(): void {
  try { sessionStorage.removeItem(PENDING_KEY); } catch { /* ignore */ }
}
