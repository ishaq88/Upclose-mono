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
// response.blob() returns Node's Blob, which jsdom's FileReader rejects on some Node versions; hand back jsdom's own Blob.
const image = (): Response => {
  const response = new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { 'Content-Type': 'image/png' } });
  response.blob = async () => new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' });
  return response;
};

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

const WITH_KITS = {
  ...SAMPLE_REPORT, session_id: 'report-session',
  additional_checks: [
    { kind: 'rig_telemetry', checked: true, source: { device: 'Synthetic pod', provenance: 'synthetic', summary: { kind: 'rig_telemetry', state: 'assessed', flags: ['rough_idle'], sample_count: 20, duration_s: 2, rpm_estimate: 800, dominant_hz: 30, irregular_windows: [] } } },
    { kind: 'obd_snapshot', checked: false, source: null },
    { kind: 'engine_audio', checked: true, source: { device: 'Synthetic clip', provenance: 'synthetic', summary: { kind: 'engine_audio', state: 'assessed', flags: [], duration_s: 3, rms_db: -20, peak_db: -3, spectrogram_sha256: 'synthetic-hash' } } },
  ],
  vibration: { state: 'assessed', reason: null, series_source: 'computed', duration_s: 2, sample_count: 20, points: [{ t_s: 0, accel_rms: 10 }, { t_s: 1, accel_rms: null }, { t_s: 2, accel_rms: 20 }] },
  mechanical_breakdown: { deductions: [{ code: 'rough_idle', label: 'Rough idle', points: '5' }], flags: [] },
  narration_audio: { label: 'Spoken synthetic report', provenance: 'synthetic', content_type: 'audio/mpeg' },
  advisory: { label: 'AI advisory, not a finding', provenance: 'fixture', status: 'available', questions_for_seller: [{ text: 'Ask about maintenance.', cites: ['rig_telemetry'] }], mechanic_checks: [], summary: 'Have a mechanic check the idle.' },
  dashboard: { assessment: { state: 'assessed', provenance: 'synthetic', lit: [{ indicator: 'check_engine', severity: 'warning', confidence: 4 }], flags: [], reason: null }, evidence_available: false, note: 'Dashboard crop withheld pending privacy review.' },
} as unknown as PaidReport;

describe('paid report kit integration', () => {
  it('renders frozen evidence, advisory and protected blob media, and revokes every blob on unmount', async () => {
    const create = vi.fn(() => `blob:private-${Math.random()}`);
    const revoke = vi.fn();
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
    const calls: Call[] = [];
    const view = render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json(WITH_KITS) }, FRAMES, { match: /\/(narration|audio|spectrogram)$/, respond: image }], calls)} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect(await screen.findByRole('heading', { name: 'Mechanical checks' })).toBeTruthy();
    expect(within(screen.getByTestId('check-obd_snapshot')).getByText('Not checked')).toBeTruthy();
    expect(screen.getByTestId('advisory').textContent).toContain('AI advisory, not a finding');
    expect(screen.getByTestId('advisory').textContent).toContain('Synthetic');
    expect(screen.getByTestId('dashboard-check').textContent).toContain('check engine');
    expect(screen.getByTestId('mechanical-breakdown').textContent).toContain('5 points deducted');
    expect(screen.getByTestId('vibration-chart').querySelector('svg')).toBeTruthy();
    expect(screen.getByTestId('vibration-chart').querySelectorAll('path')[1]!.getAttribute('d')).not.toContain('L');
    await screen.findByAltText(/Spectrogram of/);
    expect(screen.getByTestId('narration-audio').getAttribute('src')).toMatch(/^blob:/);
    const media = calls.filter((c) => /\/(narration|audio|spectrogram)$/.test(c.url));
    expect(media).toHaveLength(3);
    for (const call of media) {
      expect(call.url).not.toContain(TOKEN);
      expect(call.init).toMatchObject({ cache: 'no-store', credentials: 'omit', redirect: 'error', headers: { Authorization: `Bearer ${TOKEN}` } });
    }
    expect(calls.some((c) => /crop|dashboard\//.test(c.url))).toBe(false);
    view.unmount();
    expect(revoke).toHaveBeenCalledTimes(3);
    expect(media.every((c) => c.init?.signal?.aborted)).toBe(true);
  });

  it('shows unavailable and not-checked states without requesting absent media', async () => {
    const calls: Call[] = [];
    const audio = WITH_KITS.additional_checks.find((check) => check.kind === 'engine_audio')!;
    const report = { ...WITH_KITS, additional_checks: [{ ...audio, source: { ...audio.source!, summary: { ...audio.source!.summary, state: 'not_assessed', reason: 'clip_too_short' } } }], vibration: null, narration_audio: null, advisory: { ...WITH_KITS.advisory!, status: 'not_available', reason: 'provider_unavailable' } };
    render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json(report) }, FRAMES], calls)} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    await screen.findByRole('heading', { name: 'Mechanical checks' });
    for (const kind of ['rig_telemetry', 'obd_snapshot', 'engine_audio']) expect(within(screen.getByTestId(`check-${kind}`)).getByText('Not checked')).toBeTruthy();
    expect(screen.getByText(/Vibration chart: Not checked/)).toBeTruthy();
    expect(screen.getByTestId('advisory').textContent).toContain('Advisory unavailable');
    expect(screen.getByTestId('check-engine_audio').textContent).toContain('clip too short');
    expect(calls.some((c) => /\/(narration|audio|spectrogram)$/.test(c.url))).toBe(false);
  });

  it('never fetches paid media for a free result, and displays only dashboard counts', async () => {
    const calls: Call[] = [];
    render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json({ ...FREE, dashboard: { checked: true, flag_count: 2 } }) }], calls)} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect((await screen.findByTestId('dashboard-summary')).textContent).toContain('Checked · 2 flags');
    expect(screen.queryByTestId('advisory')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Mechanical checks' })).toBeNull();
    expect(calls).toHaveLength(1);
  });

  it('keeps written evidence when protected media fails', async () => {
    render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json(WITH_KITS) }, FRAMES, { match: /\/(narration|audio|spectrogram)$/, respond: () => problem(403, 'TOKEN_INVALID') }])} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect(await screen.findByText(/Spectrogram unavailable. The written report/)).toBeTruthy();
    expect(document.body.textContent).toContain('Ask about maintenance.');
    expect(document.querySelector('audio[src]')).toBeNull();
  });

  it('aborts pending media and never creates a blob URL after unmount', async () => {
    const create = vi.fn(() => 'blob:late');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() }));
    const api = apiFor([{ match: REPORT_URL, respond: () => json(WITH_KITS) }, FRAMES]);
    const pending: Array<(blob: Blob) => void> = [];
    const signals: AbortSignal[] = [];
    vi.spyOn(api, 'getReportMedia').mockImplementation((_slug, _which, signal) => {
      signals.push(signal!);
      return new Promise((resolve) => { pending.push(resolve); });
    });
    const view = render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    await waitFor(() => expect(pending).toHaveLength(3));
    view.unmount();
    await act(async () => { pending.forEach((resolve) => resolve(new Blob(['synthetic']))); });
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(create).not.toHaveBeenCalled();
  });

  it("renames the report's own inspection, never the one this tab may be capturing", async () => {
    const api = apiFor([{ match: REPORT_URL, respond: () => json({ ...SAMPLE_REPORT, session_id: 'report-session' }) }, FRAMES]);
    const rename = vi.spyOn(api, 'updateLabel').mockResolvedValue({ vehicle_label: 'New name' } as never);
    sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: 'tab-session', token: 'x'.repeat(43) }));
    try {
      render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
      fireEvent.click(await screen.findByRole('button', { name: 'Rename' }));
      fireEvent.change(screen.getByLabelText('Inspection name'), { target: { value: 'New name' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
      await waitFor(() => expect(rename).toHaveBeenCalledWith('report-session', 'New name'));
    } finally { sessionStorage.clear(); }
  });

  it('offers no Rename when the report does not name its inspection', async () => {
    const api = apiFor([{ match: REPORT_URL, respond: () => json({ ...SAMPLE_REPORT, session_id: null }) }, FRAMES]);
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    await screen.findByRole('heading', { name: '1 serious issue' });
    expect(screen.queryByRole('button', { name: 'Rename' })).toBeNull();
  });
});

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

  it('shows category scores on the paid and the free report when the API sends them, and none otherwise', async () => {
    const categories = {
      config_hash: 'h', label: 'Provisional index, not a diagnosis',
      visual: { state: 'assessed', score: 90, reason: null }, mechanical: { state: 'not_checked', score: null, reason: null },
      overall: { state: 'not_checked', score: null, basis: 'visual_only', note: 'Visual only: mechanical not checked.' },
    };
    const paid = render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json({ ...SAMPLE_REPORT, categories }) }, FRAMES])} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect(within(await screen.findByTestId('category-overall')).getByText('Visual only')).toBeTruthy();
    expect(within(screen.getByTestId('category-mechanical')).getByText('Not checked')).toBeTruthy();
    paid.unmount();
    const free = render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json({ ...FREE, categories }) }])} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    expect(within(await screen.findByTestId('category-visual')).getByText('90.0')).toBeTruthy();
    free.unmount();
    render(<OwnerReport api={apiFor([{ match: REPORT_URL, respond: () => json({ ...SAMPLE_REPORT }) }, FRAMES])} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    await screen.findByRole('heading', { name: '1 serious issue' });
    expect(screen.queryByTestId('category-scores')).toBeNull();
  });

  it('offers an inline Rename for the owner, and renames the visible title', async () => {
    const calls: Call[] = [];
    const sid = '11111111-1111-4111-8111-111111111111';
    const api = apiFor([
      { match: REPORT_URL, respond: () => json({ ...SAMPLE_REPORT, session_id: sid }) }, FRAMES,
      { match: /\/sessions\/[^/]+\/label$/, respond: () => json({ vehicle_label: '2014 Honda Civic' }) },
    ], calls);
    const view = render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={noop} onRefresh={noop} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Rename' }));
    fireEvent.change(screen.getByLabelText('Inspection name'), { target: { value: '2014 Honda Civic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
    expect(await screen.findByRole('heading', { name: '2014 Honda Civic', level: 1 })).toBeTruthy();
    expect(calls.find((c) => c.init?.method === 'PUT')?.url).toBe(`/api/v1/sessions/${sid}/label`);
    view.unmount();
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

  it.each([401, 404])('shows %s without retrying; only 401 reports lost access', async (status) => {
    const calls: Call[] = [];
    const api = apiFor([{ match: REPORT_URL, respond: () => problem(status, status === 401 ? 'TOKEN_INVALID' : 'NOT_FOUND') }], calls);
    const unauthorized = vi.fn();
    render(<OwnerReport api={api} slug="owner-slug" onUnauthorized={unauthorized} onRefresh={noop} pollMs={5} />);
    expect(await screen.findByRole('alert')).toBeTruthy();
    // Only a refused credential (401) reports lost access; a 404 is this report's own error and the session stays.
    if (status === 401) expect(unauthorized).toHaveBeenCalled(); else expect(unauthorized).not.toHaveBeenCalled();
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
