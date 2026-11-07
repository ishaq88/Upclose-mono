/**
 * Account sign-in persistence. Unlike the tab-bound inspection owner token (session/storage.ts), an account
 * token is meant to survive closing the tab, so it lives in localStorage with its server expiry. It is never
 * put in a URL or the DOM, and it is never used as an owner token. Every access is wrapped: storage can be blocked.
 */
const KEY = 'upclose.account.v1';

export interface StoredAccount {
  token: string;
  email: string;
  /** ISO timestamp from the server; an expired record is treated as signed out. */
  expiresAt: string;
}

function store(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function loadAccount(now: number = Date.now()): StoredAccount | null {
  try {
    const raw = store()?.getItem(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<StoredAccount>;
    if (typeof value.token !== 'string' || typeof value.email !== 'string' || typeof value.expiresAt !== 'string') return null;
    if (Date.parse(value.expiresAt) <= now) { clearAccount(); return null; }
    return { token: value.token, email: value.email, expiresAt: value.expiresAt };
  } catch {
    return null;
  }
}

export function saveAccount(account: StoredAccount): void {
  try { store()?.setItem(KEY, JSON.stringify(account)); } catch { /* signed in for this page only */ }
}

export function clearAccount(): void {
  try { store()?.removeItem(KEY); } catch { /* nothing to clear */ }
}
