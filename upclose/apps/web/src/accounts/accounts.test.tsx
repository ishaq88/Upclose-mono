// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountProvider } from './AccountContext';
import { MyReportsPage } from './MyReportsPage';
import { ResetPasswordPage } from './ResetPasswordPage';
import { SaveInspection } from './SaveInspection';
import { SignInPage } from './SignInPage';
import { nav } from './nav';
import type { StoredAccount } from './storage';

const OWNER = 'owner-token-SECRET-123';
const ACCOUNT_TOKEN = 'account-token-SECRET-456';
const RESET = 'reset-token-SECRET-789';
const SID = '11111111-1111-4111-8111-111111111111';
const signedInAccount: StoredAccount = { token: ACCOUNT_TOKEN, email: 'a@example.test', expiresAt: '2999-01-01T00:00:00Z' };

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;
function stubFetch(handler: Handler) {
  const mock = vi.fn(async (url: string, init: RequestInit) => handler(String(url), init));
  vi.stubGlobal('fetch', mock);
  return mock;
}
const json = (body: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
const auth = { account: { id: SID, email: 'new@example.test', created_at: '2026-01-01T00:00:00Z', disabled: false }, token: ACCOUNT_TOKEN, expires_at: '2999-01-01T00:00:00Z' };

function renderWith(ui: React.ReactElement, initial: StoredAccount | null = null) {
  return render(<AccountProvider initial={initial}>{ui}</AccountProvider>);
}
function fill(label: RegExp | string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
function assertNoSecrets(): void {
  for (const secret of [OWNER, ACCOUNT_TOKEN, RESET]) {
    expect(document.body.innerHTML).not.toContain(secret);
    expect(window.location.href).not.toContain(secret);
  }
}

beforeEach(() => { window.history.replaceState(null, '', '/'); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });

describe('SignInPage', () => {
  it('shows a plain-language error for wrong credentials and does not navigate', async () => {
    stubFetch(() => json({ code: 'INVALID_CREDENTIALS', detail: 'x' }, 401));
    const go = vi.spyOn(nav, 'go').mockImplementation(() => {});
    renderWith(<SignInPage />);
    fill('Email', 'a@example.test');
    fill('Password', 'wrong-password');
    fireEvent.submit(screen.getByLabelText('Email').closest('form') as HTMLFormElement);
    expect((await screen.findByRole('alert')).textContent).toMatch(/don't match/);
    expect(go).not.toHaveBeenCalled();
  });

  it('uses retry-after when rate limited', async () => {
    stubFetch(() => json({ code: 'RATE_LIMITED', detail: '' }, 429, { 'Retry-After': '30' }));
    renderWith(<SignInPage />);
    fill('Email', 'a@example.test');
    fill('Password', 'whatever-long');
    fireEvent.submit(screen.getByLabelText('Email').closest('form') as HTMLFormElement);
    expect((await screen.findByRole('alert')).textContent).toMatch(/30 seconds/);
  });

  it('validates the 10 character minimum on create, then signs in and goes to My reports', async () => {
    const fetchMock = stubFetch(() => json(auth, 201));
    const go = vi.spyOn(nav, 'go').mockImplementation(() => {});
    renderWith(<SignInPage />);
    fireEvent.click(screen.getByRole('tab', { name: 'Create account' }));
    expect(screen.getByText('At least 10 characters')).toBeTruthy();
    fill('Email', 'new@example.test');
    fill('Password', 'short');
    fireEvent.submit(screen.getByLabelText('Email').closest('form') as HTMLFormElement);
    expect((await screen.findByRole('alert')).textContent).toMatch(/at least 10/i);
    expect(fetchMock).not.toHaveBeenCalled();
    fill('Password', 'long-enough-pw');
    fireEvent.submit(screen.getByLabelText('Email').closest('form') as HTMLFormElement);
    await waitFor(() => expect(go).toHaveBeenCalledWith('/reports'));
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/accounts\/register$/);
  });

  it('shows a generic message after a reset request', async () => {
    stubFetch(() => new Response(null, { status: 202 }));
    renderWith(<SignInPage />);
    fireEvent.click(screen.getByRole('button', { name: 'Forgot password?' }));
    fill('Email', 'nobody@example.test');
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }));
    expect(await screen.findByText(/If that email has an account, we've sent a link/)).toBeTruthy();
  });
});

describe('ResetPasswordPage', () => {
  it('shows a request form when there is no token', async () => {
    renderWith(<ResetPasswordPage />);
    expect(await screen.findByText(/needs a reset link/)).toBeTruthy();
  });

  it('strips the token from the address bar, confirms, and explains all devices were signed out', async () => {
    window.history.replaceState(null, '', `/reset-password#token=${RESET}`);
    const fetchMock = stubFetch(() => new Response(null, { status: 204 }));
    renderWith(<ResetPasswordPage />);
    await screen.findByLabelText('New password');
    expect(window.location.hash).toBe('');
    assertNoSecrets();
    fill('New password', 'a-brand-new-password');
    fireEvent.submit(screen.getByLabelText('New password').closest('form') as HTMLFormElement);
    expect(await screen.findByText(/every device has been signed out/)).toBeTruthy();
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body)).token).toBe(RESET);
    expect(screen.getByRole('link', { name: 'Sign in' }).getAttribute('href')).toBe('/signin');
    assertNoSecrets();
  });

  it('offers a new link when the token is invalid', async () => {
    window.history.replaceState(null, '', `/reset-password#token=${RESET}`);
    stubFetch(() => json({ code: 'TOKEN_INVALID', detail: '' }, 400));
    renderWith(<ResetPasswordPage />);
    await screen.findByLabelText('New password');
    fill('New password', 'a-brand-new-password');
    fireEvent.submit(screen.getByLabelText('New password').closest('form') as HTMLFormElement);
    expect(await screen.findByText(/invalid or has expired/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Send reset link' })).toBeTruthy();
  });
});

describe('SaveInspection', () => {
  it('registers, then claims, when signed out; tokens stay out of the DOM and URL', async () => {
    const fetchMock = stubFetch((url) => (url.endsWith('/register') ? json(auth, 201) : json({ session_id: SID, claimed: true })));
    renderWith(<SaveInspection sessionId={SID} ownerToken={OWNER} />);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fill('Email', 'new@example.test');
    fill('Password', 'long-enough-pw');
    fireEvent.submit(screen.getByLabelText('Email').closest('form') as HTMLFormElement);
    expect(await screen.findByText(/My reports/)).toBeTruthy();
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls[0]).toMatch(/\/accounts\/register$/);
    expect(urls[1]).toMatch(/\/accounts\/claim$/);
    const claimInit = fetchMock.mock.calls[1][1];
    expect((claimInit.headers as Record<string, string>).Authorization).toBe(`Bearer ${ACCOUNT_TOKEN}`);
    expect(JSON.parse(String(claimInit.body))).toEqual({ session_id: SID, owner_token: OWNER });
    assertNoSecrets();
  });

  it('claims directly when signed in and reports a failure plainly', async () => {
    stubFetch(() => json({ code: 'STATE_CONFLICT', detail: '' }, 409));
    renderWith(<SaveInspection sessionId={SID} ownerToken={OWNER} />, signedInAccount);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save to my account' }));
    expect((await screen.findByRole('alert')).textContent).toMatch(/can't be saved/);
    assertNoSecrets();
  });

  it('closes on Escape (cancel) and restores focus to the opener', () => {
    renderWith(<SaveInspection sessionId={SID} ownerToken={OWNER} />);
    const opener = screen.getByRole('button', { name: 'Save' });
    fireEvent.click(opener);
    const dialog = document.querySelector('dialog') as HTMLDialogElement;
    fireEvent(dialog, new Event('cancel', { cancelable: true }));
    expect(document.activeElement).toBe(opener);
    expect(dialog.hasAttribute('open')).toBe(false);
  });
});

describe('MyReportsPage', () => {
  const rows = [
    { session_id: SID, state: 'finalized', report_state: 'ready', report_slug: 'slug-one', capture_mode: 'guided', part_state_counts: { processed: 6 }, purpose: 'buyer', created_at: '2026-10-01T12:00:00Z', updated_at: '2026-10-01T12:00:00Z', vehicle_label: '2014 Honda Civic', score: 88.5, overall_score: 91 },
    { session_id: '22222222-2222-4222-8222-222222222222', state: 'capturing', report_state: null, report_slug: null, capture_mode: 'guided', part_state_counts: { uploaded: 2, pending: 4 }, purpose: 'buyer', created_at: '2026-10-02T12:00:00Z', updated_at: '2026-10-02T12:00:00Z', vehicle_label: null },
    { session_id: '33333333-3333-4333-8333-333333333333', state: 'deleted', report_state: null, report_slug: null, capture_mode: 'guided', part_state_counts: {}, purpose: 'buyer', created_at: '2026-10-03T12:00:00Z', updated_at: '2026-10-03T12:00:00Z', vehicle_label: 'Gone' },
  ];

  it('prompts to sign in when signed out', () => {
    renderWith(<MyReportsPage />);
    expect(screen.getByRole('link', { name: 'Sign in' }).getAttribute('href')).toBe('/signin');
  });

  it('lists rows as links without any score, and never shows tokens', async () => {
    sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: 'other', token: OWNER }));
    stubFetch(() => json({ sessions: rows }));
    renderWith(<MyReportsPage />, signedInAccount);
    const link = await screen.findByRole('link', { name: /2014 Honda Civic/ });
    expect(link.getAttribute('href')).toBe('/report/slug-one');
    expect(screen.getByText('Inspection, Oct 2, 2026')).toBeTruthy();
    expect(screen.getByText(/Continue on the device you started on/)).toBeTruthy();
    expect(screen.getByText('Deleted')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/88\.5|91|score/i);
    expect(screen.getAllByRole('link', { name: 'New inspection' }).length).toBe(1);
    assertNoSecrets();
  });

  it('links an unfinished capture to the capture screen only on the device that started it', async () => {
    sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: rows[1].session_id, token: OWNER }));
    stubFetch(() => json({ sessions: rows }));
    renderWith(<MyReportsPage />, signedInAccount);
    const link = await screen.findByRole('link', { name: /Continue capturing/ });
    expect(link.getAttribute('href')).toBe('/');
    assertNoSecrets();
  });

  it('shows an error with retry, and signs out', async () => {
    const fetchMock = stubFetch((url) => (url.endsWith('/logout') ? new Response(null, { status: 204 }) : new Response('x', { status: 500 })));
    renderWith(<MyReportsPage />, signedInAccount);
    expect((await screen.findByRole('alert')).textContent).toMatch(/Something went wrong/);
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(screen.getByRole('link', { name: 'Sign in' })).toBeTruthy());
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/logout'))).toBe(true);
  });
});
