// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { openEngineStep } from '../testing/engineStep';
import { FakeBackend } from '../testing/fakeBackend';
import { GUIDE_STEPS } from './guide';
import { GuideViewer } from './GuideViewer';

// WebGL does not exist in jsdom: the viewer stays on its loading state and the text fallbacks carry the content.
vi.mock('./Guide3D', () => ({ default: () => null }));

let backend: FakeBackend;
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  backend = new FakeBackend();
  backend.sessionState = 'capturing';
  vi.stubGlobal('fetch', backend.fetch);
  sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

const wrapUp = () => screen.findByTestId('wrap-up');

describe('capture shell', () => {
  it('puts the filmed count in the header and a Finish action that opens the wrap-up, not the finalize call', async () => {
    backend.parts[0].state = 'processed';
    backend.parts[1].state = 'processing';
    render(<App />);
    const header = (await screen.findByRole('banner'));
    await waitFor(() => expect(within(header).getByRole('status').textContent).toBe('2 of 8 filmed · 1 checking'));
    fireEvent.click(within(header).getByRole('button', { name: 'Finish' }));
    expect(await wrapUp()).toBeTruthy();
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(0);
  });

  it('keeps the engine check out of the zone steps until its list item is chosen, and keeps its state when you look at a zone', async () => {
    render(<App />);
    const nav = await screen.findByRole('navigation', { name: 'Zones' });
    expect(screen.queryByRole('button', { name: 'Start engine check' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Engine check', level: 2 })).toBeNull();
    await openEngineStep();
    fireEvent.click(await screen.findByRole('button', { name: 'Start engine check' }));
    expect(screen.getByTestId('engine-current')).toBeTruthy();
    expect(within(nav).getByRole('button', { name: /^Engine check/ }).getAttribute('aria-current')).toBe('step');
    fireEvent.click(within(nav).getByRole('button', { name: /Passenger side/ }));
    expect(screen.getByTestId('engine-current').closest('[hidden]')).not.toBeNull(); // hidden, not gone
    expect(screen.getByRole('heading', { name: 'Passenger side', level: 3 })).toBeTruthy();
    await openEngineStep();
    expect(screen.getByTestId('engine-current').closest('[hidden]')).toBeNull();
  });

  it('links the Dashboard prompt to the engine check step', async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: /Dashboard/ }));
    fireEvent.click(within(await screen.findByTestId('engine-check-prompt')).getByRole('link', { name: 'Do the engine check first' }));
    expect(await screen.findByRole('button', { name: 'Start engine check' })).toBeTruthy();
  });
});

describe('wrap-up view', () => {
  it('shows the question, the overview car, eight coverage rows and what is not checked', async () => {
    backend.parts[0].state = 'processed';
    backend.parts[1].state = 'skipped';
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Finish capture' }));
    await wrapUp();
    expect(screen.getByRole('heading', { name: 'Ready to build the report?' })).toBeTruthy();
    expect(screen.getByText(/You can't add clips after this/)).toBeTruthy();
    const rows = within(screen.getByRole('list', { name: 'Coverage by zone' })).getAllByRole('listitem');
    expect(rows).toHaveLength(8);
    const text = (partNo: number) => screen.getByTestId(`wrap-row-${partNo}`).textContent;
    expect(text(1)).toContain('Filmed');
    expect(text(2)).toContain('Skipped');
    expect(text(3)).toContain('Not filmed, will be skipped');
    expect(text(5)).toContain('Optional, not filmed'); // the dashboard
    expect(screen.getByText(/6 zones not yet filmed will be marked skipped/)).toBeTruthy();
    expect(screen.getByTestId('missing-kits').textContent).toContain('They will show as not checked in the report.');
    expect(screen.getByRole('img', { name: /whole car in 3D/ }) ?? screen.getByTestId('guide-fallback')).toBeTruthy();
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(0);
  });

  it('opens the engine check from the not-checked notice and returns to filming with Keep filming', async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Finish capture' }));
    await wrapUp();
    fireEvent.click(screen.getByRole('button', { name: 'Open the engine check' }));
    expect(await screen.findByRole('button', { name: 'Start engine check' })).toBeTruthy();
    expect(screen.queryByTestId('wrap-up')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }));
    await wrapUp();
    fireEvent.click(screen.getByRole('button', { name: 'Keep filming' }));
    expect(screen.queryByTestId('wrap-up')).toBeNull();
    expect(screen.getByRole('navigation', { name: 'Zones' })).toBeTruthy();
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(0);
  });

  it('says why Finish is blocked on the button itself, and cannot finalize while a clip is queued', async () => {
    backend.parts[0].state = 'uploaded';
    render(<App />);
    const header = await screen.findByRole('banner');
    fireEvent.click(await within(header).findByRole('button', { name: 'Finish' }));
    await wrapUp();
    const blocked = screen.getByRole('button', { name: 'Waiting for 1 clip to start' }) as HTMLButtonElement;
    expect(blocked.disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Finish and build report' })).toBeNull();
    expect(screen.getByTestId('finish-waiting').textContent).toContain('Waiting for 1 video to start processing');
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(0);
  });

  it('finishes with one explicit action and then shows the frozen, finished state', async () => {
    backend.parts[0].state = 'processed';
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Finish capture' }));
    await wrapUp();
    fireEvent.click(screen.getByRole('button', { name: 'Finish and build report' }));
    await screen.findByText(/Capture is finished/);
    expect(backend.callsTo('POST', '/finalize')).toHaveLength(1);
    expect(screen.queryByTestId('wrap-up')).toBeNull();
  });
});

describe('GuideViewer overview', () => {
  it('describes the whole car instead of a zone, and falls back to a text line without 3D', async () => {
    render(<GuideViewer guide={GUIDE_STEPS[0]!} done={[1, 2]} overview />);
    expect(screen.getByRole('img', { name: /whole car in 3D/ })).toBeTruthy();
    expect(screen.getByText(/Filmed zones are tinted/)).toBeTruthy();
    expect(screen.queryByText(/Start at the rear corner/)).toBeNull();
  });
});
