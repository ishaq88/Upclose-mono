import type { components } from '../generated/contracts';
import { API_BASE } from '../config';
import { ApiError, parseRetryAfter, type FetchLike } from '../api/client';

type Schemas = components['schemas'];
export type AuthResponse = Schemas['AuthResponse'];
export type AccountSummary = Schemas['AccountSummary'];
export type AccountSessionSummary = Schemas['AccountSessionSummary'];
export type ClaimSessionResponse = Schemas['ClaimSessionResponse'];

/** Account API. An account token is a different authority from an inspection owner token and is sent only here. */
export interface AccountApi {
  register(email: string, password: string): Promise<AuthResponse>;
  login(email: string, password: string): Promise<AuthResponse>;
  logout(): Promise<void>;
  me(): Promise<AccountSummary>;
  changePassword(currentPassword: string, newPassword: string): Promise<void>;
  /** Always resolves the same way whether or not the email has an account. */
  requestPasswordReset(email: string): Promise<void>;
  confirmPasswordReset(token: string, newPassword: string): Promise<void>;
  /** Links an anonymous inspection to the signed-in account; needs that inspection's owner token. */
  claim(sessionId: string, ownerToken: string): Promise<ClaimSessionResponse>;
  sessions(): Promise<AccountSessionSummary[]>;
}

export interface AccountApiOptions {
  getToken: () => string | null;
  fetchImpl?: FetchLike;
  baseUrl?: string;
}

export function createAccountApi(options: AccountApiOptions): AccountApi {
  const base = `${options.baseUrl ?? API_BASE}/accounts`;
  const doFetch: FetchLike = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));

  async function call<T>(method: string, path: string, body?: unknown, auth = true): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const token = auth ? options.getToken() : null;
    if (token) headers.Authorization = `Bearer ${token}`;
    let response: Response;
    try {
      response = await doFetch(`${base}${path}`, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store',
      });
    } catch {
      throw new ApiError(0, 'NETWORK', 'The server could not be reached.');
    }
    if (!response.ok) {
      let code: ApiError['code'] = 'UNKNOWN', detail = '';
      try {
        const problem = (await response.json()) as { code?: string; detail?: string };
        if (typeof problem.code === 'string') code = problem.code as ApiError['code'];
        if (typeof problem.detail === 'string') detail = problem.detail;
      } catch { /* not a problem document */ }
      throw new ApiError(response.status, code, detail, parseRetryAfter(response.headers.get('Retry-After')));
    }
    if (response.status === 204 || response.status === 202) return undefined as T;
    return (await response.json()) as T;
  }

  return {
    register: (email, password) => call('POST', '/register', { email, password }, false),
    login: (email, password) => call('POST', '/login', { email, password }, false),
    logout: () => call('POST', '/logout'),
    me: () => call('GET', '/me'),
    changePassword: (current_password, new_password) => call('POST', '/password', { current_password, new_password }),
    requestPasswordReset: (email) => call('POST', '/password-reset/request', { email }, false),
    confirmPasswordReset: (token, new_password) => call('POST', '/password-reset/confirm', { token, new_password }, false),
    claim: (session_id, owner_token) => call('POST', '/claim', { session_id, owner_token }),
    sessions: async () => (await call<{ sessions: AccountSessionSummary[] }>('GET', '/sessions')).sessions,
  };
}
