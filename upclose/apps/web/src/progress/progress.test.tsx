// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { ApiError, createApi } from '../api/client';
import { CheckoutReturn } from '../components/CheckoutReturn';
import { ProgressPanel, isSafeCheckoutUrl } from '../components/ProgressPanel';
import { useSession } from '../session/useSession';
import { FakeBackend, syntheticVerdict } from '../testing/fakeBackend';
import { isTerminal, nextPollDelay } from './polling';
import { describeReason } from './reasons';

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

function video(bytes: number): File {
  return new File([new Uint8Array(bytes).map((_, i) => (i + 3) & 255)], 'clip.mp4', { type: 'video/mp4' });
}

function apiForTests() {
  return createApi({ getToken: () => backend.token, fetchImpl: backend.fetch });
}

function hookFor() {
  const api = apiForTests();
  return renderHook(() => useSession(api, backend.sessionId, 20));
}

async function openVerdict(): Promise<HTMLElement> {
  const slot = await screen.findByTestId('free-verdict-slot');
  await waitFor(() => expect(screen.getByTestId('verdict-headline')).toBeTruthy());
  const details = slot.querySelector('details')!;
  details.open = true;
  return slot;
}

describe('free verdict card', () => {
  it('appears while another part is uploading without touching its controls, focus or the record area', async () => {
    backend.holdPuts = true;
    seedSession();
    render(<App />);
    fireEvent.click(within(await screen.findByRole('navigation', { name: 'Zones' })).getByRole('button', { name: /Passenger side/ }));
    const card = (await screen.findByRole('heading', { level: 3, name: /^Passenger side/ })).closest('article')!;
    fireEvent.change(within(card).getByLabelText(/Upload passenger side clip/), { target: { files: [video(2000)] } });
    fireEvent.click(await within(card).findByRole('button', { name: 'Upload this clip' }));
    const cancel = await within(card).findByRole('button', { name: 'Cancel and retake' });
    const slotBefore = screen.getByTestId('free-verdict-slot');
    expect(slotBefore.textContent).toContain('Appears here once the Driver side clip');
    cancel.focus();

    backend.parts[0].state = 'processed';
    backend.freeVerdict = syntheticVerdict();
    await waitFor(() => expect(screen.getByTestId('verdict-headline').textContent).toContain('Provisional index 94.0'), { timeout: 5000 });

    // Same section node filled in place, same upload control node, focus unchanged, no modal.
    expect(screen.getByTestId('free-verdict-slot')).toBe(slotBefore);
    expect(within(card).getByRole('button', { name: 'Cancel and retake' })).toBe(cancel);
    expect(document.activeElement).toBe(cancel);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    // Below the active zone card, so filling it never pushes the record controls down.
    expect(card.compareDocumentPosition(slotBefore) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect((slotBefore.querySelector('details') as HTMLDetailsElement).open).toBe(false);
  }, 15000);

  it('shows score as provisional index, finding details, verbatim confidence label, synthetic note and withheld evidence', async () => {
    backend.parts[0].state = 'processed';
    backend.freeVerdict = syntheticVerdict();
    seedSession();
    render(<App />);
    const slot = await openVerdict();
    expect(within(slot).getByText(/Provisional visual-condition index/)).toBeTruthy();
    expect(within(slot).getByTestId('part1-score').textContent).toContain('94.0');
    expect(within(slot).getByTestId('verdict-headline').textContent).toContain('1 finding');
    const finding = within(slot).getByTestId('free-finding');
    expect(finding.textContent).toContain('Rust');
    expect(finding.textContent).toContain('severity 2');
    expect(within(finding).getByText('Synthetic fixture verdict; no real inference was performed.')).toBeTruthy();
    expect(within(finding).getByTestId('confidence-label').textContent).toBe('Uncalibrated model confidence, not an accuracy probability');
    expect(finding.textContent).toContain('Evidence image withheld');
    expect(within(slot).getByTestId('synthetic-badge')).toBeTruthy();
    expect(within(slot).getByTestId('coverage-state').textContent).toContain('Assessed');
    expect(within(slot).getByTestId('part1-summary').textContent).toContain('1 visible finding');
    expect(slot.querySelector('img')).toBeNull();
    expect(backend.calls.filter((call) => /image|frame|evidence/i.test(call.url))).toHaveLength(0);
  });

  it('never shows 0 or 100 for a null score: it says Not scored with the coverage reason', async () => {
    backend.parts[0].state = 'processed';
    backend.freeVerdict = syntheticVerdict({
      part1_score: null, findings: [],
      coverage: [{ zone: 'driver_side', state: 'partial', reason: 'Required concept coverage is missing or incomplete.' }],
    });
    seedSession();
    render(<App />);
    const slot = await openVerdict();
    expect(within(slot).getByTestId('part1-score').textContent).toBe('Not scored');
    expect(within(slot).getByTestId('not-scored-reason').textContent).toBe('Required concept coverage is missing or incomplete.');
    expect(within(slot).getByTestId('verdict-headline').textContent).toContain('Not scored');
    expect(slot.textContent).not.toMatch(/\b(0|100)(\.0)? out of 100/);
    expect(within(slot).getByTestId('no-findings').textContent).toContain('not a clean result');
    expect(within(slot).getByTestId('no-findings').textContent).not.toContain('No visible findings detected');
  });

  it('uses the exact no-findings phrase for an assessed zone and never says damage free', async () => {
    backend.parts[0].state = 'processed';
    backend.freeVerdict = syntheticVerdict({
      findings: [], part1_score: 100,
      coverage: [{ zone: 'driver_side', state: 'assessed', reason: 'No visible findings detected in assessed footage.' }],
      part1_summary: 'Driver side: no visible findings detected in assessed footage.',
    });
    seedSession();
    render(<App />);
    const slot = await openVerdict();
    expect(within(slot).getByTestId('no-findings').textContent).toBe('No visible findings detected in assessed footage.');
    expect(slot.textContent?.toLowerCase()).not.toContain('damage free');
    expect(slot.textContent?.toLowerCase()).not.toContain('damage-free');
    expect(within(slot).queryByTestId('synthetic-badge')).toBeNull();
  });

  it('shows locked zones as totals with a lock icon and no per-concept detail', async () => {
    backend.parts[0].state = 'processed';
    backend.freeVerdict = syntheticVerdict({ locked_zones: [
      { zone: 'passenger_side', finding_count: 2, locked: true },
      { zone: 'tires', finding_count: 1, locked: true },
    ] });
    seedSession();
    render(<App />);
    const slot = await openVerdict();
    const passenger = within(slot).getByTestId('locked-passenger_side');
    expect(passenger.textContent).toContain('Passenger side');
    expect(passenger.textContent).toContain('2 findings');
    expect(within(passenger).getByRole('img', { name: 'Locked' })).toBeTruthy();
    expect(within(slot).getByTestId('locked-tires').textContent).toContain('1 finding');
    expect(passenger.textContent).not.toMatch(/rust|severity|dent|scratch/i);
  });

  it('explains placeholders honestly when Part 1 is skipped or failed', async () => {
    backend.parts[0].state = 'failed';
    backend.parts[0].reason = 'INGEST_JOB_DEAD';
    seedSession();
    render(<App />);
    const slot = await screen.findByTestId('free-verdict-slot');
    await waitFor(() => expect(slot.textContent).toContain('Not scored'));
    expect(slot.textContent).toContain('stopped after repeated attempts');
    expect(slot.textContent).toContain('not a clean result');
  });
});

describe('reason codes', () => {
  it('humanizes known, MEDIA_*, and unknown codes without leaking unsafe strings', () => {
    expect(describeReason('INGEST_JOB_DEAD')).toContain('stopped after repeated attempts');
    expect(describeReason('MEDIA_UNSUPPORTED_CODEC')).toContain('encoding is not supported');
    expect(describeReason('MEDIA_SOMETHING_NEW')).toContain('could not be processed');
    expect(describeReason('SOME_NEW_CODE')).toContain('(code SOME_NEW_CODE)');
    expect(describeReason('<script>alert(1)</script>')).not.toContain('script');
    expect(describeReason(null)).toContain('no reason given');
    for (const code of ['INGEST_JOB_DEAD', 'MEDIA_CORRUPT', 'X']) expect(describeReason(code)).toContain('not a clean result');
  });

  it('shows the humanized reason in the status list for a failed part', async () => {
    backend.parts[2].state = 'failed';
    backend.parts[2].reason = 'MEDIA_CORRUPT';
    seedSession();
    render(<App />);
    const reason = await screen.findByTestId('progress-reason-3');
    expect(reason.textContent).toContain('damaged');
    expect(reason.textContent).not.toContain('MEDIA_CORRUPT');
  });
});

describe('report state and checkout', () => {
  async function finalizedApp(state: 'building' | 'ready' | 'failed' | null): Promise<void> {
    backend.sessionState = 'finalized';
    backend.parts.forEach((part) => { part.state = 'skipped'; });
    backend.report = state === null ? null : { slug: 'slug-abc', state, unlocked: false };
    seedSession();
    render(<App />);
    await screen.findByRole('heading', { name: 'Processing and report' });
  }

  it('shows building, then a authenticated report button and unlock button once ready', async () => {
    await finalizedApp('building');
    await screen.findByText(/Your report is being built/);
    expect(screen.queryByRole('button', { name: /^Unlock full report/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Open your free report' })).toBeNull();

    backend.report = { slug: 'slug-abc', state: 'ready', unlocked: false };
    const button = await screen.findByRole('button', { name: 'Open your free report' }, { timeout: 5000 });
    expect(button.getAttribute('href')).toBeNull();
    expect(document.body.innerHTML).not.toContain(backend.token);
    expect(screen.getByRole('button', { name: /^Unlock full report/ })).toBeTruthy();
  }, 15000);

  it('shows an honest message when the report failed and offers no unlock', async () => {
    await finalizedApp('failed');
    expect((await screen.findByRole('alert')).textContent).toContain('could not be built');
    expect(screen.queryByRole('button', { name: /^Unlock full report/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Open your free report' })).toBeNull();
  });

  it('does not call checkout until the report is ready; then posts only the product and navigates', async () => {
    await finalizedApp('ready');
    const navigate = vi.fn();
    cleanup();
    const api = apiForTests();
    render(<ProgressPanel api={api} progress={{ state: 'finalized', free_verdict: null, report: { slug: 'slug-abc', state: 'ready', unlocked: false }, parts: backend.parts }}
      onRefresh={() => {}} onUnauthorized={() => {}} navigate={navigate} />);
    expect(backend.callsTo('POST', '/checkout')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: /^Unlock full report/ }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('https://checkout.test/c/cs_test_123'));
    const [call] = backend.callsTo('POST', '/checkout');
    expect(call.url).toBe('/api/v1/reports/slug-abc/checkout');
    expect(call.body).toEqual({ product: 'report_unlock' });
    expect(call.headers.Authorization).toBe(`Bearer ${backend.token}`);
  });

  it('never offers checkout while building or without a report', () => {
    for (const report of [null, { slug: 's', state: 'building' as const, unlocked: false }, { slug: 's', state: 'failed' as const, unlocked: false }]) {
      const { unmount } = render(<ProgressPanel api={apiForTests()} progress={{ state: 'finalized', free_verdict: null, report, parts: [] }}
        onRefresh={() => {}} onUnauthorized={() => {}} />);
      expect(screen.queryByRole('button', { name: /^Unlock full report/ })).toBeNull();
      unmount();
    }
  });

  it('handles 409 REPORT_NOT_READY by explaining and refreshing, without navigating', async () => {
    const navigate = vi.fn();
    const onRefresh = vi.fn();
    backend.report = { slug: 'slug-abc', state: 'building', unlocked: false }; // the page believes ready, the server disagrees
    render(<ProgressPanel api={apiForTests()} progress={{ state: 'finalized', free_verdict: null, report: { slug: 'slug-abc', state: 'ready', unlocked: false }, parts: [] }}
      onRefresh={onRefresh} onUnauthorized={() => {}} navigate={navigate} />);
    fireEvent.click(screen.getByRole('button', { name: /^Unlock full report/ }));
    expect((await screen.findByText('The report is not ready yet')).closest('[role="alert"]')).toBeTruthy();
    expect(navigate).not.toHaveBeenCalled();
    expect(onRefresh).toHaveBeenCalled();
  });

  it('refuses to navigate to an unsafe checkout address', async () => {
    backend.report = { slug: 'slug-abc', state: 'ready', unlocked: false };
    backend.checkoutUrl = 'javascript:alert(1)';
    const navigate = vi.fn();
    render(<ProgressPanel api={apiForTests()} progress={{ state: 'finalized', free_verdict: null, report: { slug: 'slug-abc', state: 'ready', unlocked: false }, parts: [] }}
      onRefresh={() => {}} onUnauthorized={() => {}} navigate={navigate} />);
    fireEvent.click(screen.getByRole('button', { name: /^Unlock full report/ }));
    await screen.findByText('Checkout could not be opened');
    expect(navigate).not.toHaveBeenCalled();
    expect(isSafeCheckoutUrl('https://checkout.stripe.com/c/pay/x')).toBe(true);
    expect(isSafeCheckoutUrl('http://localhost:4242/pay')).toBe(true);
    expect(isSafeCheckoutUrl('http://evil.test/pay')).toBe(false);
    expect(isSafeCheckoutUrl('not a url')).toBe(false);
  });

  it('treats a lost token at checkout as unauthorized', async () => {
    backend.report = { slug: 'slug-abc', state: 'ready', unlocked: false };
    backend.unauthorized = true;
    const onUnauthorized = vi.fn();
    render(<ProgressPanel api={apiForTests()} progress={{ state: 'finalized', free_verdict: null, report: { slug: 'slug-abc', state: 'ready', unlocked: false }, parts: [] }}
      onRefresh={() => {}} onUnauthorized={onUnauthorized} />);
    fireEvent.click(screen.getByRole('button', { name: /^Unlock full report/ }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });
});

describe('checkout return page', () => {
  afterEach(() => vi.useRealTimers());

  it('grants nothing, reads nothing from the address, calls no API, then routes home without a remembered report', () => {
    vi.useFakeTimers();
    window.history.pushState({}, '', '/checkout/return?result=success&session_id=cs_test_SECRET');
    const navigate = vi.fn();
    try {
      render(<CheckoutReturn navigate={navigate} redirectMs={1000} />);
      expect(screen.getByRole('heading', { name: 'Checking your payment…' })).toBeTruthy();
      const text = document.body.textContent ?? '';
      expect(text).toContain('Opening this page does not unlock anything');
      expect(text).not.toMatch(/payment (was )?successful|you(?:'ve| have) unlocked|unlocked!|thank you for your purchase/i);
      expect(text).not.toContain('cs_test_SECRET');
      expect(screen.queryByRole('button', { name: /unlock/i })).toBeNull();
      expect(navigate).not.toHaveBeenCalled();
      act(() => { vi.advanceTimersByTime(1000); });
      expect(navigate).toHaveBeenCalledWith('/');
      expect(backend.calls).toHaveLength(0);
    } finally {
      window.history.pushState({}, '', '/');
    }
  });

  it('routes back to the report that was sent to checkout', () => {
    vi.useFakeTimers();
    sessionStorage.setItem('upclose.checkout.pending.v1', 'slug-abc');
    const navigate = vi.fn();
    render(<CheckoutReturn navigate={navigate} redirectMs={500} />);
    expect(screen.getByRole('link', { name: 'Back to your report' }).getAttribute('href')).toBe('/report/slug-abc');
    act(() => { vi.advanceTimersByTime(500); });
    expect(navigate).toHaveBeenCalledWith('/report/slug-abc');
    expect(backend.calls).toHaveLength(0);
  });

  it('renders identically for result=cancel and result=success, and is wired into the app route', () => {
    const textFor = (query: string): string => {
      window.history.pushState({}, '', `/checkout/return${query}`);
      const { unmount } = render(<App />);
      const text = document.querySelector('section')!.textContent!;
      unmount();
      return text;
    };
    try {
      expect(textFor('?result=success')).toBe(textFor('?result=cancel'));
      expect(textFor('?result=cancel')).toContain('Checking your payment');
    } finally {
      window.history.pushState({}, '', '/');
    }
  });
});

describe('progress stages and zones', () => {
  const part = (n: number, zone: string, state: string, reason: string | null = null) => ({ id: `00000000-0000-4000-8000-00000000000${n}`, part_no: n, zone, state, reason, active_upload_id: null }) as never;

  it('shows stages only as far as the data supports and puts optional zones last, tagged', () => {
    const progress = { state: 'finalized', free_verdict: null, report: null, parts: [
      part(8, 'interior', 'pending'), part(1, 'driver_side', 'processed'), part(7, 'dash_electrical', 'pending'), part(2, 'passenger_side', 'processing'),
    ] } as never;
    render(<ProgressPanel api={apiForTests()} progress={progress} onRefresh={() => {}} onUnauthorized={() => {}} />);
    const stages = within(screen.getByRole('list', { name: 'Progress' })).getAllByRole('listitem');
    expect(stages.map((li) => li.textContent?.replace(' (done)', ''))).toEqual(['Uploaded', 'Car found', 'Checking damage', 'Report']);
    expect(stages[0].className).toBe('on');
    expect(stages[1].className).toBe('on');
    expect(stages[2].getAttribute('aria-current')).toBe('step');
    expect(stages[3].className).toBe('');
    const rows = within(screen.getByRole('list', { name: 'Processing status by zone' })).getAllByRole('listitem').map((li) => li.textContent);
    expect(rows[2]).toContain('Dashboard');
    expect(rows[2]).toContain('Optional');
    expect(rows[3]).toContain('Interior');
    expect(rows[3]).toContain('Optional');
    expect(rows[0]).not.toContain('Optional');
  });

  it('shows no stage as current before anything reached the server, and no invented progress', () => {
    const progress = { state: 'capturing', free_verdict: null, report: null, parts: [part(1, 'driver_side', 'pending')] } as never;
    render(<ProgressPanel api={apiForTests()} progress={progress} onRefresh={() => {}} onUnauthorized={() => {}} />);
    const stages = within(screen.getByRole('list', { name: 'Progress' })).getAllByRole('listitem');
    expect(stages.some((li) => li.className !== '')).toBe(false);
    expect(document.querySelector('[aria-current="step"]')).toBeNull();
  });

  it('offers film-again guidance for a blur/quality failure and plain reason text otherwise', () => {
    const progress = { state: 'finalized', free_verdict: null, report: null, parts: [
      part(1, 'driver_side', 'failed', 'MEDIA_TOO_BLURRY'), part(2, 'passenger_side', 'failed', 'INGEST_TIMEOUT'),
    ] } as never;
    render(<ProgressPanel api={apiForTests()} progress={progress} onRefresh={() => {}} onUnauthorized={() => {}} />);
    expect(screen.getByTestId('progress-part-1').textContent).toContain('Too blurry');
    expect(screen.getByTestId('progress-reason-1').textContent).toContain('Film this zone again');
    expect(screen.getByTestId('progress-part-2').textContent).toContain('Could not check');
    expect(screen.getByTestId('progress-reason-2').textContent).toContain('took too long');
    expect(screen.getByTestId('progress-reason-2').textContent).not.toContain('Film this zone again');
    expect(screen.getByTestId('progress-reason-2').textContent).toContain('not a clean result');
  });
});

describe('polling', () => {
  it('computes exponential back-off, honours Retry-After, and caps both', () => {
    expect(nextPollDelay({ baseMs: 3000, failures: 0, retryAfterSeconds: null })).toBe(3000);
    expect(nextPollDelay({ baseMs: 3000, failures: 1, retryAfterSeconds: null })).toBe(6000);
    expect(nextPollDelay({ baseMs: 3000, failures: 2, retryAfterSeconds: null })).toBe(12000);
    expect(nextPollDelay({ baseMs: 3000, failures: 9, retryAfterSeconds: null })).toBe(30000);
    expect(nextPollDelay({ baseMs: 3000, failures: 1, retryAfterSeconds: 45 })).toBe(45000);
    expect(nextPollDelay({ baseMs: 3000, failures: 1, retryAfterSeconds: 99999 })).toBe(120000);
  });

  it('is terminal only when the session is going away or the report left building', () => {
    expect(isTerminal(null, null)).toBe(false);
    expect(isTerminal({ state: 'capturing' }, { state: 'capturing', report: null })).toBe(false);
    expect(isTerminal({ state: 'finalized' }, { state: 'finalized', report: null })).toBe(false);
    expect(isTerminal({ state: 'finalized' }, { state: 'finalized', report: { slug: 's', state: 'building', unlocked: false } })).toBe(false);
    expect(isTerminal({ state: 'finalized' }, { state: 'finalized', report: { slug: 's', state: 'ready', unlocked: false } })).toBe(false);
    expect(isTerminal({ state: 'finalized' }, { state: 'finalized', report: { slug: 's', state: 'failed', unlocked: false } })).toBe(true);
    expect(isTerminal({ state: 'deleted' }, null)).toBe(true);
  });

  it('polls session and progress together, repeatedly, from one loop', async () => {
    const { result } = hookFor();
    await waitFor(() => expect(backend.callsTo('GET', '/progress').length).toBeGreaterThanOrEqual(3));
    expect(result.current.progress?.parts).toHaveLength(8);
    expect(result.current.session?.parts).toHaveLength(8);
    expect(backend.callsTo('GET', '/progress').length).toBeLessThanOrEqual(backend.callsTo('GET', `/sessions/${backend.sessionId}`).length + 1);
  });

  it('keeps polling ready reports to observe entitlement changes', async () => {
    backend.sessionState = 'finalized';
    backend.report = { slug: 'slug-abc', state: 'ready', unlocked: false };
    hookFor();
    await waitFor(() => expect(backend.callsTo('GET', '/progress')).toHaveLength(1));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 150)); });
    expect(backend.callsTo('GET', '/progress').length).toBeGreaterThan(1);
  });

  it('backs off after a 429 and does not poll again before Retry-After', async () => {
    backend.progressScript = [{ status: 429, retryAfter: '1' }];
    const { result } = hookFor();
    await waitFor(() => expect(result.current.error?.title).toBe('Too many requests'));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 400)); });
    expect(backend.callsTo('GET', '/progress')).toHaveLength(1);
    // A user-triggered refresh cannot hammer through the back-off either.
    act(() => { result.current.refresh(); result.current.refresh(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(backend.callsTo('GET', '/progress')).toHaveLength(1);
  });

  it('backs off exponentially on repeated 5xx and recovers to the base interval', async () => {
    backend.progressScript = [{ status: 503 }, { status: 503 }, { status: 503 }];
    const { result } = hookFor();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)); });
    // Without back-off 300 ms at 20 ms would be ~15 reads; failures at 40, 80, 160 ms cap it well below that.
    expect(backend.callsTo('GET', '/progress').length).toBeLessThanOrEqual(6);
    await waitFor(() => expect(result.current.error).toBeNull(), { timeout: 3000 });
    expect(result.current.progress).not.toBeNull();
  });

  it('clears previously loaded state when an auth failure races another read failure', async () => {
    const api = apiForTests();
    const { result } = renderHook(() => useSession(api, backend.sessionId, 20));
    await waitFor(() => expect(result.current.session).not.toBeNull());
    vi.spyOn(api, 'getSession').mockRejectedValue(new ApiError(503, 'INTERNAL', ''));
    vi.spyOn(api, 'getProgress').mockRejectedValue(new ApiError(401, 'TOKEN_INVALID', ''));
    await waitFor(() => expect(result.current.unauthorized).toBe(true));
    expect(result.current.session).toBeNull();
    expect(result.current.progress).toBeNull();
  });

  it('stops on 401 and flags the session unauthorized', async () => {
    backend.unauthorized = true;
    const { result } = hookFor();
    await waitFor(() => expect(result.current.unauthorized).toBe(true));
    const count = backend.callsTo('GET', '/progress').length;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(backend.callsTo('GET', '/progress')).toHaveLength(count);
    expect(new ApiError(401, 'TOKEN_INVALID', '').status).toBe(401);
  });
});
