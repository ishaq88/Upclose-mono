// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { createApi, type FreeReport, type PaidReport } from '../api/client';
import { SAMPLE_REPORT } from '../sample/sample';
import { App } from '../App';
import { FakeBackend } from '../testing/fakeBackend';
import { OwnerReport } from './OwnerReport';
import { ProgressPanel } from './ProgressPanel';

vi.mock('../report/TopDownMap', () => ({ default: () => <div data-testid="map" /> }));

class FakeObserver { observe(): void {} disconnect(): void {} }
beforeAll(() => { vi.stubGlobal('IntersectionObserver', FakeObserver); });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.stubGlobal('IntersectionObserver', FakeObserver);
  sessionStorage.clear();
  localStorage.clear();
});
const noop = (): void => {};
const TOKEN = 'private-token-xyz';
const FRAME_ID = SAMPLE_REPORT.findings[0]!.id;

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const problem = (status: number, code: string): Response => json({ code, detail: '', status, title: code, request_id: 'r' }, status);
const image = (): Response => new Response(new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' }), { status: 200 });

const FREE = {
  slug: 'owner-slug', revision_id: '11111111-1111-4111-8111-111111111111', scope: 'free', state: 'ready', vehicle_label: null,
  part1_score: 94.96, part1_summary: 'Driver side: 1 visible finding.',
  coverage: [{ zone: 'driver_side', state: 'assessed', reason: null }],
  findings: [{
    id: FRAME_ID, zone: 'driver_side', concept: 'rust', severity: 3, confidence: 0.7,
    confidence_label: 'Uncalibrated model confidence, not an accuracy probability', evidence_note: 'Flaking paint at the arch.',
    subzone: 'rocker', component_id: null, flags: ['needs_review'], bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, evidence_available: true,
    source_timestamp_ms: null, mapping_basis: null,
  }],
  locked_zones: [{ zone: 'tires', finding_count: 2, locked: true }],
} as unknown as FreeReport;

interface Call { url: string; init?: RequestInit }
interface Route { match: RegExp; respond: () => Response | Promise<Response> }
function apiFor(routes: Route[], calls: Call[] = []) {
  return createApi({
    getToken: () => TOKEN, baseUrl: '/api/v1',
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      const route = routes.find((r) => r.match.test(url));
      if (!route) throw new Error(`unexpected request ${url}`);
      return route.respond();
    },
  });
}
const REPORT_URL = /\/api\/v1\/reports\/owner-slug$/;
const FRAMES = { match: /\/frames\//, respond: image };

describe('owner report (JSON)', () => {
  it('renders a paid report from JSON, loading frames lazily with the token in a header only', async () => {
    const calls: Call[] = [];
    const api = apiFor([{ match: REPORT_URL, respond: () => json(SAMPLE_REPORT) }, FRAMES], calls);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect(await screen.findByRole('heading', { name: '1 serious issue' })).toBeTruthy();
    await waitFor(() => expect(document.querySelectorAll('.ev-frame img').length).toBeGreaterThan(0));
    const frameCall = calls.find((c) => c.url.includes('/frames/'))!;
    expect(frameCall.url).toBe(`/api/v1/reports/owner-slug/frames/${FRAME_ID}`);
    expect((frameCall.init?.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
    expect(calls.some((c) => c.url.startsWith('/r/'))).toBe(false);
    for (const call of calls) expect(call.url).not.toContain(TOKEN);
    expect(document.querySelector('iframe')).toBeNull();
    expect(document.documentElement.outerHTML).not.toContain(TOKEN);
    expect(sessionStorage.length + localStorage.length).toBe(0);
    expect(screen.getByRole('button', { name: 'Share with mechanic' })).toBeTruthy();
  });

  it('shows a free report with its unlock panel, never requests evidence, and checks out on click', async () => {
    const calls: Call[] = [];
    const api = apiFor([
      { match: REPORT_URL, respond: () => json(FREE) },
      { match: /\/checkout$/, respond: () => json({ checkout_url: 'https://checkout.test/c/1', unlocked: false }) },
    ], calls);
    const navigate = vi.fn();
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} navigate={navigate} />);
    expect((await screen.findByTestId('verdict-line')).textContent).toBe('1 serious issue');
    expect(screen.getByTestId('part1-score').textContent).toContain('94.9');
    expect(screen.getByTestId('part1-score').textContent).not.toContain('95');
    expect(screen.getByTestId('locked-tires').textContent).toContain('2 findings');
    expect(screen.queryByRole('button', { name: 'Share with mechanic' })).toBeNull();
    expect(document.querySelector('img')).toBeNull();
    expect(calls.some((c) => /frames|\/r\//.test(c.url))).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Unlock full report · $19' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('https://checkout.test/c/1'));
    expect(sessionStorage.getItem('upclose.checkout.pending.v1')).toBe('owner-slug');
  });

  it('polls a building report with the status view, then shows the paid report', async () => {
    let n = 0;
    const api = apiFor([
      { match: REPORT_URL, respond: () => (++n < 3 ? json({ slug: 'owner-slug', revision_id: 'r', scope: 'paid', state: 'building' }) : json(SAMPLE_REPORT)) },
      FRAMES,
    ]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} pollMs={5} />);
    expect(await screen.findByText('Building your report')).toBeTruthy();
    expect(await screen.findByRole('heading', { name: '1 serious issue' })).toBeTruthy();
    expect(n).toBe(3);
  });

  it('treats 409 REPORT_NOT_READY as building, not as an error', async () => {
    const api = apiFor([{ match: REPORT_URL, respond: () => problem(409, 'REPORT_NOT_READY') }]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} pollMs={100000} />);
    expect(await screen.findByText('Building your report')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says a failed report has nothing to open or unlock', async () => {
    const api = apiFor([{ match: REPORT_URL, respond: () => json({ slug: 'owner-slug', revision_id: 'r', scope: 'free', state: 'failed' }) }]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect((await screen.findByRole('alert')).textContent).toContain('could not be built');
    expect(screen.queryByRole('button', { name: /Unlock/ })).toBeNull();
  });

  it('names a network failure, offers retry, and recovers', async () => {
    let fail = true;
    const api = createApi({
      getToken: () => TOKEN, baseUrl: '/api/v1',
      fetchImpl: async () => { if (fail) throw new TypeError('offline'); return json(FREE); },
    });
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} pollMs={100000} />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: 'Retry report' })).toBeTruthy();
    fail = false;
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry report' }));
    expect(await screen.findByTestId('verdict-line')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it.each([401, 404])('treats %s as lost access: tells the user and does not retry', async (status) => {
    const calls: Call[] = [];
    const api = apiFor([{ match: REPORT_URL, respond: () => problem(status, status === 401 ? 'TOKEN_INVALID' : 'NOT_FOUND') }], calls);
    const unauthorized = vi.fn();
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={unauthorized} onRefresh={noop} pollMs={5} />);
    await waitFor(() => expect(unauthorized).toHaveBeenCalled());
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry report' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Sign in' })).toBeTruthy();
    await act(async () => { await new Promise((r) => setTimeout(r, 40)); });
    expect(calls).toHaveLength(1);
  });

  it('labels a synthetic paid report and keeps the label for synthetic free results', async () => {
    const synthetic: PaidReport = { ...SAMPLE_REPORT, findings: SAMPLE_REPORT.findings.map((f) => ({ ...f, evidence_note: 'Synthetic fixture verdict; no real inference was performed.' })) };
    const api = apiFor([{ match: REPORT_URL, respond: () => json(synthetic) }, FRAMES]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect((await screen.findByTestId('synthetic-badge')).textContent).toContain('Synthetic test result');
    cleanup();
    const freeApi = apiFor([{ match: REPORT_URL, respond: () => json({ ...FREE, part1_summary: 'Synthetic fixture: 1 finding.' }) }]);
    render(<OwnerReport api={freeApi} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect(await screen.findByTestId('synthetic-badge')).toBeTruthy();
  });

  it('withholds a frame that fails to load instead of showing a raw route', async () => {
    const api = apiFor([{ match: REPORT_URL, respond: () => json(SAMPLE_REPORT) }, { match: /\/frames\//, respond: () => problem(404, 'NOT_FOUND') }]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    await screen.findByRole('heading', { name: '1 serious issue' });
    await waitFor(() => expect(screen.getAllByText('Image could not be loaded').length).toBeGreaterThan(0));
    expect(document.querySelector('.ev-frame img')).toBeNull();
  });

  it('keeps re-reading a free report after a checkout return, and says nothing is unlocked until verified', async () => {
    sessionStorage.setItem('upclose.checkout.pending.v1', 'owner-slug');
    let n = 0;
    const api = apiFor([{ match: REPORT_URL, respond: () => (++n < 3 ? json(FREE) : json(SAMPLE_REPORT)) }, FRAMES]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} pollMs={5} />);
    expect(await screen.findByText(/Confirming your payment/)).toBeTruthy();
    expect(await screen.findByRole('heading', { name: '1 serious issue' })).toBeTruthy();
    expect(sessionStorage.getItem('upclose.checkout.pending.v1')).toBeNull();
  });
});

describe('share with mechanic', () => {
  const setup = (extra: Route[] = []) => apiFor([{ match: REPORT_URL, respond: () => json(SAMPLE_REPORT) }, FRAMES, ...extra]);

  it('enables sharing, copies the link without any token, and can turn it off', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    const api = setup([{ match: /\/share$/, respond: () => json({ public_access_active: true, share_enabled: true, url: '/r/owner-slug' }) }]);
    const disable = vi.spyOn(api, 'disableShare').mockResolvedValue();
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Share with mechanic' }));
    const input = (await screen.findByLabelText('Share link')) as HTMLInputElement;
    expect(input.value).toBe(`${window.location.origin}/r/owner-slug`);
    expect(input.value).not.toContain(TOKEN);
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(input.value));
    expect(await screen.findByText('Link copied.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Stop sharing' }));
    await waitFor(() => expect(disable).toHaveBeenCalledWith('owner-slug'));
    expect(await screen.findByRole('button', { name: 'Share with mechanic' })).toBeTruthy();
  });

  it('falls back to selecting the link when the clipboard is unavailable', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined });
    document.execCommand = vi.fn(() => true);
    const api = setup([{ match: /\/share$/, respond: () => json({ public_access_active: false, share_enabled: true, url: '/r/owner-slug' }) }]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Share with mechanic' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Copy link' }));
    await waitFor(() => expect(document.execCommand).toHaveBeenCalledWith('copy'));
    expect(screen.getByText(/public sharing is switched off/)).toBeTruthy();
  });

  it('names a refusal for a non-owner and offers recovery', async () => {
    const api = setup([{ match: /\/share$/, respond: () => problem(403, 'TOKEN_INVALID') }]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Share with mechanic' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Only the owner can share');
  });
});

describe('opening the report from the progress panel', () => {
  it('goes to the report page instead of rendering it inline', () => {
    const navigate = vi.fn();
    const api = apiFor([]);
    const progress = { state: 'finalized' as const, parts: [], free_verdict: null, report: { slug: 'owner-slug', state: 'ready' as const, unlocked: true } };
    render(<ProgressPanel api={api} onUnauthorized={noop} onRefresh={noop} navigate={navigate} progress={progress} />);
    expect(screen.queryByRole('button', { name: /Unlock full report/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Open your full report' }));
    expect(navigate).toHaveBeenCalledWith('/report/owner-slug');
    expect(screen.queryByTestId('owner-report')).toBeNull();
  });
});

describe('owner media deletion', () => {
  function setup(): FakeBackend {
    const backend = new FakeBackend();
    sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
    vi.stubGlobal('fetch', backend.fetch);
    return backend;
  }
  it('requires explicit confirmation, posts bearer and empty object, then forgets ownership', async () => {
    const backend = setup();
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete session media' }));
    expect(backend.callsTo('POST', '/delete-media')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Keep session' }));
    expect(screen.queryByRole('button', { name: 'Yes, delete session media' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Delete session media' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete session media' }));
    await screen.findByRole('button', { name: /^Continue with/ });
    expect(backend.callsTo('POST', '/delete-media')).toEqual([expect.objectContaining({ body: {}, headers: expect.objectContaining({ Authorization: `Bearer ${backend.token}` }) })]);
    expect(sessionStorage.getItem('upclose.session.v1')).toBeNull();
    expect(document.body.innerHTML).not.toContain(backend.token);
  });
  it('aborts an active transfer before requesting media deletion', async () => {
    const backend = setup();
    backend.holdPuts = true;
    render(<App />);
    const input = await screen.findByLabelText(/Upload driver side clip/);
    const file = new File([new Uint8Array(32)], 'clip.mp4', { type: 'video/mp4' });
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole('button', { name: 'Upload this clip' }));
    await waitFor(() => expect(backend.calls.some((call) => call.url.startsWith('https://storage.test/'))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Delete session media' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete session media' }));
    await screen.findByRole('button', { name: /^Continue with/ });
    expect(backend.callsTo('POST', '/delete-media')).toHaveLength(1);
    expect(backend.callsTo('POST', '/complete')).toHaveLength(0);
  });
  it('keeps ownership and allows retry when the deletion request fails', async () => {
    const backend = setup();
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => url.endsWith('/delete-media') ? new Response('{}', { status: 503 }) : backend.fetch(url, init));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete session media' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete session media' }));
    await screen.findByRole('button', { name: 'Delete session media' });
    expect(screen.getByText('The service had a problem')).toBeTruthy();
    expect(sessionStorage.getItem('upclose.session.v1')).not.toBeNull();
  });
});
