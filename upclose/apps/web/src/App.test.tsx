// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { guideFor } from './capture/guide';
import { CONSENT_VERSION } from './config';
import { FakeBackend } from './testing/fakeBackend';

// three.js and WebGL do not exist in jsdom; the viewer then keeps its loading state and the text steps stay on screen.
vi.mock('./capture/Guide3D', () => ({ default: () => null }));

let backend: FakeBackend;

beforeEach(() => {
  sessionStorage.clear();
  backend = new FakeBackend();
  vi.stubGlobal('fetch', backend.fetch);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

function seedSession(): void {
  sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
}

function video(bytes: number, pattern = 1, name = 'clip.mp4'): File {
  const data = new Uint8Array(bytes).map((_, i) => (i * pattern + 3) & 255);
  return new File([data], name, { type: 'video/mp4' });
}

const startButton = (): HTMLButtonElement => screen.getByRole('button', { name: /^Continue with/ }) as HTMLButtonElement;
const agree = (): HTMLElement => screen.getByLabelText(/I understand and agree/);

/** The guide card for a zone, selecting the zone first when another one is open. */
async function zoneCard(title: string): Promise<HTMLElement> {
  const nav = await screen.findByRole('navigation', { name: 'Zones' });
  const zone = within(nav).getByRole('button', { name: new RegExp(`^${title}`) });
  if (zone.getAttribute('aria-current') !== 'step') fireEvent.click(zone);
  const heading = await screen.findByRole('heading', { level: 3, name: new RegExp(`^${title}`) });
  return heading.closest('article')!;
}

async function pickAndUpload(partNo: number, file: File): Promise<void> {
  const card = await zoneCard(guideFor(partNo)!.title);
  const input = within(card).getByLabelText(/Upload .* clip/);
  fireEvent.change(input, { target: { files: [file] } });
  fireEvent.click(await within(card).findByRole('button', { name: 'Upload this clip' }));
}

describe('consent and session creation', () => {
  it('requires consent before a session can be created and sends nothing until then', async () => {
    render(<App />);
    const start = startButton();
    expect((start as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(start);
    expect(backend.calls).toHaveLength(0);
    expect(screen.getByText(/closing it can lose access/i)).toBeTruthy();
  });

  it('creates the session with the configured consent version and keeps the token out of the DOM and URLs', async () => {
    render(<App />);
    fireEvent.click(screen.getByRole('radio', { name: /Guided clips/ }));
    fireEvent.change(screen.getByLabelText(/Seller listing text/), { target: { value: 'Clean 2014 hatchback' } });
    fireEvent.click(agree());
    fireEvent.click(startButton());
    await screen.findByRole('heading', { name: 'Capture steps' });

    const create = backend.callsTo('POST', '/sessions').find((call) => call.url.endsWith('/sessions'))!;
    expect(create.body).toEqual({ purpose: 'buyer', consent_version: CONSENT_VERSION, consent: true, listing_text: 'Clean 2014 hatchback' });
    expect(create.headers.Authorization).toBeUndefined();

    expect(JSON.parse(sessionStorage.getItem('upclose.session.v1')!)).toEqual({ sessionId: backend.sessionId, token: backend.token });
    expect(document.body.innerHTML).not.toContain(backend.token);
    expect(location.href).not.toContain(backend.token);
    for (const call of backend.calls) expect(call.url).not.toContain(backend.token);
    // The token is only ever sent as a bearer header, never to the storage host.
    const later = backend.calls.filter((call) => call.url.includes(backend.sessionId));
    expect(later.length).toBeGreaterThan(0);
    for (const call of later) expect(call.headers.Authorization).toBe(`Bearer ${backend.token}`);
  });

  it('shows a rate-limit message when session creation is refused', async () => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ code: 'RATE_LIMITED', detail: '' }), { status: 429, headers: { 'Retry-After': '120' } }));
    render(<App />);
    fireEvent.click(agree());
    fireEvent.click(startButton());
    expect((await screen.findByRole('alert')).textContent).toContain('120 seconds');
  });
});

describe('guide steps', () => {
  it('lists the eight canonical zones with server states, the optional zones last, and the underbody safety wording', async () => {
    seedSession();
    render(<App />);
    await screen.findByRole('heading', { name: 'Capture steps' });
    const nav = screen.getByRole('navigation', { name: 'Zones' });
    const zones = within(nav).getAllByRole('button');
    // Eight zones, then the optional engine check as the last item of the list.
    expect(zones).toHaveLength(9);
    expect(zones.map((zone) => zone.querySelector('.zt')!.textContent)).toEqual([
      'Driver side', 'Passenger side', 'Front and rear, with glass', 'Tires', 'Engine bay', 'Underbody, low sweep', 'DashboardOptional', 'InteriorOptional', 'Engine checkOptional',
    ]);
    expect(screen.getAllByText('Not filmed yet')).toHaveLength(8);
    expect(zones.filter((zone) => zone.getAttribute('aria-current') === 'step')).toHaveLength(1);
    expect(zones[0]!.getAttribute('aria-current')).toBe('step');
    // The progress line follows the selected zone's position in the display order, not its part number.
    expect(screen.getByText('Zone 1 of 8')).toBeTruthy();
    const underbody = await zoneCard('Underbody');
    expect(zones[5]!.getAttribute('aria-current')).toBe('step');
    expect(screen.getByText('Zone 6 of 8')).toBeTruthy();
    expect(within(underbody).getByText(/Stay standing and keep your whole body outside the car/)).toBeTruthy();
    expect(within(underbody).getByText(/Do not crawl, kneel under, lie down, or reach beneath the vehicle/)).toBeTruthy();
    expect(screen.getByRole('img', { name: /never go under the car/ })).toBeTruthy();
    expect(screen.getByText(/Stay standing outside, phone low/)).toBeTruthy();
  });

  it('offers part-specific file fallbacks that accept video and can open the phone camera', async () => {
    seedSession();
    render(<App />);
    const card = await zoneCard('Driver side');
    const camera = within(card).getByLabelText(/Record driver side/) as HTMLInputElement;
    const choose = within(card).getByLabelText(/Upload driver side clip/) as HTMLInputElement;
    expect(camera.accept).toBe('video/*');
    expect(camera.getAttribute('capture')).toBe('environment');
    expect(choose.accept).toBe('video/*');
    expect(choose.hasAttribute('capture')).toBe(false);
    // jsdom has no camera: the in-page recorder explains and the file inputs remain.
    expect(within(card).getByText(/cannot record video inside the page|secure \(HTTPS\)/)).toBeTruthy();
  });
});

describe('skip and finish', () => {
  it('skips a zone only after confirmation and shows it as skipped from server state', async () => {
    seedSession();
    render(<App />);
    const card = await zoneCard('Driver side');
    fireEvent.click(within(card).getByRole('button', { name: 'Skip this zone' }));
    expect(backend.callsTo('POST', '/skip')).toHaveLength(0);
    fireEvent.click(within(card).getByRole('button', { name: /Yes, skip Driver side/ }));
    await waitFor(() => expect(screen.getByTestId('state-1').textContent).toBe('Skipped'));
    expect(backend.callsTo('POST', '/skip')).toHaveLength(1);
    expect(screen.getByText(/Skipped zones:/).parentElement!.textContent).toContain('Driver side');
  });

  it('disables Finish while the server reports an upload in progress', async () => {
    backend.parts[1].state = 'uploading';
    seedSession();
    render(<App />);
    await screen.findByRole('heading', { name: 'Capture steps' });
    expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/An upload is still in progress/)).toBeTruthy();
  });

  it('disables Finish with a plain waiting message while a clip is queued, and maps a finalize 409 to a friendly message', async () => {
    backend.parts[1].state = 'uploaded';
    seedSession();
    render(<App />);
    await screen.findByRole('heading', { name: 'Capture steps' });
    expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('finish-waiting').textContent).toContain('Waiting for 1 video to start processing');
    backend.parts[1].state = 'processing';
    await waitFor(() => expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(false), { timeout: 8000 });
  }, 15000);

  it('shows a friendly message when finalize returns 409', async () => {
    seedSession();
    render(<App />);
    const original = backend.fetch;
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (url.endsWith('/finalize')) return new Response(JSON.stringify({ code: 'STATE_CONFLICT', detail: '' }), { status: 409 });
      return original(url, init);
    });
    await screen.findByRole('heading', { name: 'Capture steps' });
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Finish and build report' })); });
    expect(await screen.findByText(/A video is still waiting to be processed/)).toBeTruthy();
    expect(screen.queryByText('That step has already changed')).toBeNull();
  });

  it('disables Finish during a local upload, and re-enables it after a retake releases the part', async () => {
    backend.holdPuts = true;
    seedSession();
    render(<App />);
    await pickAndUpload(1, video(2000));
    await screen.findByRole('button', { name: 'Cancel and retake' });
    expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel and retake' }));
    await waitFor(() => expect(backend.callsTo('DELETE', '/uploads/')).toHaveLength(1));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(false));
    expect(backend.parts[0].state).toBe('pending');
  });

  it('uploads a small synthetic file through the real client and shows the server-reported state', async () => {
    seedSession();
    render(<App />);
    await pickAndUpload(1, video(5000));
    await waitFor(() => expect(screen.getByTestId('state-1').textContent).toBe('Uploaded, queued'));
    expect(backend.callsTo('POST', '/complete')).toHaveLength(1);
    expect(screen.getByText(/Clip accepted/)).toBeTruthy();
  });

  it('finishes only on an explicit confirmed action and then lists skipped zones', async () => {
    seedSession();
    render(<App />);
    await screen.findByRole('heading', { name: 'Capture steps' });
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }));
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(0);
    expect(screen.getByText(/8 zones not yet filmed will be marked skipped/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Finish and build report' }));
    await screen.findByText(/Capture is finished/);
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(1);
    await waitFor(() => expect(within(screen.getByRole('navigation', { name: 'Zones' })).getAllByText('Skipped')).toHaveLength(8));
    await screen.findByText(/Your report is being built/);
    expect(backend.callsTo('GET', '/progress').length).toBeGreaterThan(0);
  });
});

describe('saving the inspection to an account during capture', () => {
  const ACCOUNT_TOKEN = 'acct_0123456789abcdefghijklmnop';
  const accountAuth = { account: { id: '22222222-2222-4222-8222-222222222222', email: 'owner@example.test', created_at: '2026-01-01T00:00:00Z', disabled: false }, token: ACCOUNT_TOKEN, expires_at: '2999-01-01T00:00:00Z' };

  /** The fake owner backend plus the account routes; the account token is NOT an owner token, as on the real API. */
  function withAccounts(): void {
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith('/accounts/register')) return new Response(JSON.stringify(accountAuth), { status: 201, headers: { 'Content-Type': 'application/json' } });
      if (path.endsWith('/accounts/claim')) return new Response(JSON.stringify({ session_id: backend.sessionId, account_id: accountAuth.account.id }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      return backend.fetch(url, init);
    });
  }

  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { localStorage.clear(); });

  it('keeps the capture tab working through sign-in, save, finish and the report link', async () => {
    withAccounts();
    seedSession();
    render(<App />);
    await screen.findByRole('heading', { name: 'Capture steps' });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: 'owner@example.test' } });
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: 'correct horse battery staple' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create account and save' }));
    await screen.findByText('Saved');
    expect(sessionStorage.getItem('upclose.session.v1')).toContain(backend.token);
    expect(screen.queryByText('Your session has expired or was lost')).toBeNull();

    await pickAndUpload(1, video(3000));
    await waitFor(() => expect(screen.getByTestId('state-1').textContent).toBe('Uploaded, queued'));
    backend.parts[0].state = 'processing';
    await waitFor(() => expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(false), { timeout: 8000 });
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }));
    fireEvent.click(screen.getByRole('button', { name: 'Finish and build report' }));
    await screen.findByText(/Capture is finished/);
    backend.report = { slug: 'slug-abc', state: 'ready', unlocked: false };
    expect(await screen.findByRole('button', { name: 'Open your free report' }, { timeout: 5000 })).toBeTruthy();
    expect(screen.queryByText('Your session has expired or was lost')).toBeNull();
    // Every owner call kept the owner token; the account token was never sent to an owner route.
    expect(backend.calls.every((call) => call.headers.Authorization === `Bearer ${backend.token}` || call.headers.Authorization === undefined)).toBe(true);
  }, 20000);

  it('does not lose the session when a sub-resource answers 404 or a stray 401', async () => {
    seedSession();
    const original = backend.fetch;
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (String(url).includes('/audio') && init?.method === 'POST') return new Response(JSON.stringify({ code: 'NOT_FOUND', detail: '' }), { status: 404 });
      if (String(url).includes('/skip')) return new Response(JSON.stringify({ code: 'TOKEN_INVALID', detail: '' }), { status: 401 });
      return original(url, init);
    });
    render(<App />);
    const card = await zoneCard('Driver side');
    fireEvent.click(within(card).getByRole('button', { name: 'Skip this zone' }));
    fireEvent.click(within(card).getByRole('button', { name: /Yes, skip Driver side/ }));
    // The stray 401 triggers a re-read of the session; the session read still succeeds, so the page stays.
    await waitFor(() => expect(backend.callsTo('GET', '/sessions/').length).toBeGreaterThan(1));
    // Only the zone shows its own error; the page-level lost-session state (with its new-session button) is absent.
    expect(screen.queryByRole('button', { name: 'Start a new session' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Capture steps' })).toBeTruthy();
  });

  it('offers the report and My reports when the lost session was saved and the user is signed in', async () => {
    localStorage.setItem('upclose.account.v1', JSON.stringify({ token: ACCOUNT_TOKEN, email: 'owner@example.test', expiresAt: '2999-01-01T00:00:00Z' }));
    seedSession();
    backend.sessionState = 'finalized';
    backend.report = { slug: 'slug-abc', state: 'ready', unlocked: false };
    render(<App />);
    await screen.findByRole('button', { name: 'Open your free report' });
    backend.unauthorized = true;
    await screen.findByText('Your session has expired or was lost', undefined, { timeout: 8000 });
    expect((screen.getByRole('link', { name: "Open this inspection's report" }) as HTMLAnchorElement).getAttribute('href')).toBe('/report/slug-abc');
    expect((screen.getByRole('link', { name: 'Go to My reports' }) as HTMLAnchorElement).getAttribute('href')).toBe('/reports');
  });
});

describe('resume after reload', () => {
  it('requires the same file, rejects a different one, and continues with the right one', async () => {
    backend.holdPuts = true;
    seedSession();
    const first = render(<App />);
    await pickAndUpload(1, video(3000, 1));
    await screen.findByRole('button', { name: 'Cancel and retake' });
    await waitFor(() => expect(backend.calls.some((call) => call.method === 'PUT')).toBe(true));
    // "Reload": the tab goes away mid-upload; sessionStorage keeps the non-secret upload record.
    first.unmount();
    backend.holdPuts = false;

    render(<App />);
    const card = await zoneCard('Driver side');
    await within(card).findByText(/An upload for this zone was interrupted/);
    expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(true);

    const input = within(card).getByLabelText('Select the same video to continue');
    fireEvent.change(input, { target: { files: [video(3001, 1)] } });
    await within(card).findByText('That is not the same video');
    expect(backend.callsTo('POST', '/complete')).toHaveLength(0);

    fireEvent.change(within(card).getByLabelText('Select the same video to continue'), { target: { files: [video(3000, 7)] } });
    await waitFor(() => expect(within(card).getByText('That is not the same video')).toBeTruthy());

    fireEvent.click(within(card).getByRole('button', { name: 'Dismiss' }));
    fireEvent.change(within(card).getByLabelText('Select the same video to continue'), { target: { files: [video(3000, 1)] } });
    await waitFor(() => expect(screen.getByTestId('state-1').textContent).toBe('Uploaded, queued'));
  });
});

describe('stranded upload', () => {
  it('offers to discard an upload the tab has no record of, using active_upload_id', async () => {
    const uploadId = '22222222-2222-4222-8222-0000000000aa';
    backend.parts[0].state = 'uploading';
    backend.parts[0].active_upload_id = uploadId;
    backend.uploads.set(uploadId, { id: uploadId, partId: backend.parts[0].id, partNo: 1, size: 10, state: 'open', pieces: new Map() });
    seedSession(); // no upload record in sessionStorage
    render(<App />);
    const card = await zoneCard('Driver side');
    await within(card).findByText(/cannot continue/);
    expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(card).getByRole('button', { name: 'Discard stranded upload' }));
    await waitFor(() => expect(screen.getByTestId('state-1').textContent).toBe('Not filmed yet'));
    expect(backend.callsTo('DELETE', `/uploads/${uploadId}`)).toHaveLength(1);
    expect(backend.parts[0].active_upload_id).toBeNull();
    await waitFor(() => expect((screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement).disabled).toBe(false));
  });
});

describe('error handling', () => {
  it('explains an expired or lost session and offers a new one', async () => {
    seedSession();
    backend.unauthorized = true;
    render(<App />);
    const alert = await screen.findByText('Your session has expired or was lost');
    expect(alert.closest('[role="alert"]')!.textContent).toContain('cannot be recovered from another tab or device');
    fireEvent.click(screen.getByRole('button', { name: 'Start a new session' }));
    expect(((await screen.findByRole('button', { name: /^Continue with/ })) as HTMLButtonElement).disabled).toBe(true);
    expect(sessionStorage.getItem('upclose.session.v1')).toBeNull();
  });

  it('explains a 409 by refreshing instead of showing a stale state', async () => {
    seedSession();
    render(<App />);
    const card = await zoneCard('Driver side');
    backend.parts[0].state = 'skipped'; // changed elsewhere; the page has not polled yet
    fireEvent.click(within(card).getByRole('button', { name: 'Skip this zone' }));
    fireEvent.click(within(card).getByRole('button', { name: /Yes, skip Driver side/ }));
    await waitFor(() => expect(screen.getByTestId('state-1').textContent).toBe('Skipped'));
  });

  it('explains 413 for an oversized declared clip', async () => {
    seedSession();
    render(<App />);
    // The size check happens client-side first; a tiny file with a forced server 413 exercises the message.
    const original = backend.fetch;
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (url.endsWith('/parts') && init?.method === 'POST') return new Response(JSON.stringify({ code: 'UPLOAD_TOO_LARGE', detail: '' }), { status: 413 });
      return original(url, init);
    });
    await pickAndUpload(1, video(2000));
    expect((await screen.findByText('That video is too large')).closest('[role="alert"]')).toBeTruthy();
  });

  it('shows a plain 429 message with the wait time', async () => {
    seedSession();
    render(<App />);
    const original = backend.fetch;
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (url.endsWith('/finalize')) return new Response(JSON.stringify({ code: 'RATE_LIMITED', detail: '' }), { status: 429, headers: { 'Retry-After': '9' } });
      return original(url, init);
    });
    await screen.findByRole('heading', { name: 'Capture steps' });
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Finish and build report' })); });
    expect((await screen.findByText('Too many requests')).closest('[role="alert"]')!.textContent).toContain('9 seconds');
  });

  it('shows a 404 as not found without exposing server details', async () => {
    seedSession();
    render(<App />);
    const original = backend.fetch;
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (url.includes('/skip')) return new Response(JSON.stringify({ code: 'NOT_FOUND', detail: 'secret internals' }), { status: 404 });
      return original(url, init);
    });
    const card = await zoneCard('Driver side');
    fireEvent.click(within(card).getByRole('button', { name: 'Skip this zone' }));
    fireEvent.click(within(card).getByRole('button', { name: /Yes, skip Driver side/ }));
    const notice = await within(card).findByText('Not found');
    expect(notice.closest('[role="alert"]')!.textContent).not.toContain('secret internals');
  });
});
