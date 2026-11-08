// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { FakeBackend } from '../testing/fakeBackend';
import { rigCommand } from './kits';
import { QUALITY_WARNING_LINE } from './voiceGuide';

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

describe('capture flow, round 2', () => {
  it('says the kits feed the Mechanical score, not that they leave the score unchanged', async () => {
    render(<App />);
    const card = await screen.findByTestId('capture-kits');
    expect(card.textContent).toContain('feed the Mechanical score');
    expect(card.textContent).not.toContain('does not change the condition score');
  });

  it('shows the engine check and the engine-audio upload with test ids, and no token in the command', async () => {
    render(<App />);
    expect(await screen.findByTestId('engine-check')).toBeTruthy();
    expect(screen.getByTestId('engine-check-start')).toBeTruthy();
    const upload = screen.getByTestId('engine-audio-upload');
    expect(within(upload).getByTestId('engine-audio-record-input')).toBeTruthy();
    expect(within(upload).getByTestId('engine-audio-file-input')).toBeTruthy();
    fireEvent.click(screen.getByText('Connect the sensor pod'));
    expect(screen.getByText(rigCommand(backend.sessionId))).toBeTruthy();
    expect(rigCommand(backend.sessionId)).toBe(`python -m upclose_rig demo-run --session ${backend.sessionId} --api http://localhost:8000 --port COM3 --audio synth`);
    expect(screen.getByTestId('rig-token-note').textContent).toContain('UPCLOSE_OWNER_TOKEN');
    expect(document.body.textContent).not.toContain(backend.token);
  });

  it('offers the engine check at the Dashboard zone only', async () => {
    render(<App />);
    await screen.findByTestId('engine-check');
    expect(screen.queryByTestId('engine-check-prompt')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Dashboard/ }));
    const prompt = await screen.findByTestId('engine-check-prompt');
    expect(within(prompt).getByRole('link', { name: 'Do the engine check first' }).getAttribute('href')).toBe('#engine-check');
  });

  it('maps the client quality warnings to catalog lines', () => {
    expect(QUALITY_WARNING_LINE).toEqual({ too_short: 'clip_too_short', too_long: 'clip_too_long', low_resolution: 'low_resolution' });
  });
});
