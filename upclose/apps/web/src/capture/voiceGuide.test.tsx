// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { FakeBackend } from '../testing/fakeBackend';
import { loadVoicePreference, saveVoicePreference } from './voiceGuide';

vi.mock('./Guide3D', () => ({ default: () => null }));

const played: string[] = [];
class FakeAudio {
  src = '';
  onended: (() => void) | null = null;
  pause = vi.fn();
  play = vi.fn(() => { played.push(this.src); return Promise.resolve(); });
}

let backend: FakeBackend;
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  played.length = 0;
  backend = new FakeBackend();
  backend.sessionState = 'capturing';
  vi.stubGlobal('fetch', backend.fetch);
  vi.stubGlobal('Audio', FakeAudio);
  URL.createObjectURL = vi.fn(() => `blob:line-${played.length}`);
  URL.revokeObjectURL = vi.fn();
  sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); sessionStorage.clear(); localStorage.clear(); });

const audioFetches = () => backend.calls.filter((call) => /\/voice-guide\/[^/]+$/.test(call.url));
const current = () => screen.getByTestId('engine-current');
const caption = () => screen.getByTestId('engine-caption').textContent;
const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next step' }));

describe('engine check with spoken guidance', () => {
  it('walks the steps, speaking each from the tap and always showing its caption', async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start engine check' }));
    expect(caption()).toBe('Switch the pod on while the engine is off.');
    await waitFor(() => expect(played).toHaveLength(1));
    expect(audioFetches().map((call) => call.url)).toEqual(['/api/v1/voice-guide/engine_pod_on']);
    next();
    expect(caption()).toBe('Now start the engine.');
    next();
    expect(caption()).toContain('learning for twenty seconds');
    expect(screen.queryByRole('button', { name: 'Next step' })).toBeNull();
  });

  it('counts down the 20 s learning step, then moves on to the baseline step', async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start engine check' }));
    next();
    next();
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Start 20 s learning' }));
      expect(screen.getByTestId('engine-countdown').textContent).toBe('20 s of 20 s');
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.getByTestId('engine-countdown').textContent).toBe('15 s of 20 s');
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(caption()).toBe('Baseline learned.');
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the pod status from the sources the API lists', async () => {
    backend.sources = [{
      source_id: '22222222-2222-4222-8222-222222222222', kind: 'rig_telemetry', provenance: 'synthetic', device: 'pod',
      captured_at: '2026-11-07T21:14:03Z', data_sha256: 'a'.repeat(64), summary: { state: 'assessed' },
    }];
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start engine check' }));
    await waitFor(() => expect(within(current()).getByTestId('engine-kit-rig_telemetry').textContent).toContain('attached (Simulated)'));
  });

  it('speaks nothing when the voice guide is toggled off, and remembers the choice', async () => {
    render(<App />);
    fireEvent.click(await screen.findByLabelText('Voice guide'));
    expect(localStorage.getItem('upclose.voiceGuide.v1')).toBe('off');
    fireEvent.click(screen.getByRole('button', { name: 'Start engine check' }));
    next();
    expect(caption()).toBe('Now start the engine.');
    expect(audioFetches()).toHaveLength(0);
    expect(played).toHaveLength(0);
    cleanup();
    render(<App />);
    expect((await screen.findByLabelText('Voice guide') as HTMLInputElement).checked).toBe(false);
  });

  it('shows the caption only when the audio cannot be fetched, and never blocks the step', async () => {
    backend.voiceAudioStatus = 502;
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start engine check' }));
    expect(caption()).toBe('Switch the pod on while the engine is off.');
    await waitFor(() => expect(screen.getByTestId('voice-caption').textContent).toContain('captions only'));
    expect(played).toHaveLength(0);
    next();
    expect(caption()).toBe('Now start the engine.');
  });

  it('falls back to the local caption when the catalog does not load', async () => {
    backend.voiceCatalogStatus = 500;
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start engine check' }));
    expect(caption()).toBe('Switch the pod on while the engine is off.');
  });
});

describe('guided zones', () => {
  it('speaks a zone only when the user taps it, and keeps the zone usable if audio fails', async () => {
    render(<App />);
    await screen.findByRole('button', { name: 'Start engine check' });
    expect(audioFetches()).toHaveLength(0); // the zone opened on load is not a tap
    fireEvent.click(screen.getByRole('button', { name: /Passenger side/ }));
    await waitFor(() => expect(audioFetches().map((call) => call.url)).toEqual(['/api/v1/voice-guide/zone_passenger_side']));
    expect(screen.getByTestId('voice-caption').textContent).toContain('Start at the front corner');
    backend.voiceAudioStatus = 502;
    fireEvent.click(screen.getByRole('button', { name: /Driver side/ }));
    await waitFor(() => expect(screen.getByTestId('voice-caption').textContent).toContain('captions only'));
    expect(screen.getByRole('heading', { name: 'Driver side' })).toBeTruthy();
  });
});

describe('voice preference storage', () => {
  it('defaults to on and survives blocked storage', () => {
    expect(loadVoicePreference()).toBe(true);
    saveVoicePreference(false);
    expect(loadVoicePreference()).toBe(false);
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadVoicePreference()).toBe(true);
    expect(() => saveVoicePreference(false)).not.toThrow();
  });
});
