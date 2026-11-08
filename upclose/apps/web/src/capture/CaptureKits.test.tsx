// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { ApiError, createApi, type Api, type CaptureSource } from '../api/client';
import { FakeBackend } from '../testing/fakeBackend';
import { openEngineStep } from '../testing/engineStep';
import { useCaptureSources } from './useCaptureSources';

vi.mock('./Guide3D', () => ({ default: () => null }));

const pod: CaptureSource = {
  source_id: '22222222-2222-4222-8222-222222222222', kind: 'rig_telemetry', provenance: 'synthetic', device: 'grove-beginner-kit',
  captured_at: '2026-11-07T21:14:03Z', data_sha256: 'a'.repeat(64), summary: { state: 'assessed', flag_count: 1 },
};
const obd: CaptureSource = { ...pod, source_id: '33333333-3333-4333-8333-333333333333', kind: 'obd_snapshot', provenance: 'cached', device: 'elm327-emulator', summary: { state: 'not_assessed', flag_count: 0 } };

let backend: FakeBackend;
beforeEach(() => {
  sessionStorage.clear();
  backend = new FakeBackend();
  backend.sessionState = 'capturing';
  vi.stubGlobal('fetch', backend.fetch);
  sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); sessionStorage.clear(); });

const row = (id: string) => within(screen.getByTestId(`kit-${id}`));

describe('Capture kits card', () => {
  it('renders attached, provenance and not-assessed status from mocked sources', async () => {
    backend.sources = [pod, obd];
    render(<App />);
    await openEngineStep();
    await waitFor(() => expect(row('rig_telemetry').getByText('Attached')).toBeTruthy());
    expect(row('rig_telemetry').getByText('Simulated')).toBeTruthy();
    expect(row('obd_snapshot').getByText('Replayed')).toBeTruthy();
    expect(row('obd_snapshot').getByText('Not assessed')).toBeTruthy();
    expect(row('engine_audio').getByText('Waiting')).toBeTruthy();
    expect(row('phone_video').getByText('Waiting')).toBeTruthy();
  });

  it('treats a 404 from an API without the route as no sources', async () => {
    backend.sourcesStatus = 404;
    render(<App />);
    await openEngineStep();
    await screen.findByTestId('kit-rig_telemetry');
    expect(row('rig_telemetry').getByText('Waiting')).toBeTruthy();
    const api = createApi({ getToken: () => backend.token, fetchImpl: backend.fetch });
    await expect(api.listSources(backend.sessionId)).resolves.toEqual([]);
    backend.unauthorized = true;
    await expect(api.listSources(backend.sessionId)).rejects.toBeInstanceOf(ApiError);
  });

  it('never renders the owner token, and copies it to the clipboard only on request', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    const view = render(<App />);
    await openEngineStep();
    fireEvent.click(await screen.findByText('Connect the sensor pod'));
    expect(screen.getByText(/Paste it only into your own rig/)).toBeTruthy();
    expect(screen.getByText(`python -m upclose_rig demo-run --session ${backend.sessionId} --api http://localhost:8000 --port COM3 --audio synth`)).toBeTruthy();
    expect(view.container.innerHTML).not.toContain(backend.token);
    expect(document.body.textContent).not.toContain(backend.token);
    fireEvent.click(screen.getByRole('button', { name: 'Copy owner token' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(backend.token));
    await screen.findByRole('button', { name: 'Copied' });
    expect(view.container.innerHTML).not.toContain(backend.token);
  });

  it('lists the kits that are not attached in the finish confirmation', async () => {
    backend.sources = [pod];
    render(<App />);
    await waitFor(() => expect(row('rig_telemetry').getByText('Attached')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Finish capture' }));
    const notice = screen.getByTestId('missing-kits');
    expect(notice.textContent).toContain('Phone video, OBD, Engine audio');
    expect(notice.textContent).not.toContain('Sensor pod');
    expect(notice.textContent).toContain('They will show as not checked in the report.');
  });
});

describe('useCaptureSources polling', () => {
  function Probe({ api, polling }: { api: Api; polling: boolean }) {
    return <p data-testid="n">{useCaptureSources(api, 's', polling, 5000).length}</p>;
  }

  it('polls every 5 s while capturing and stops after finalize', async () => {
    vi.useFakeTimers();
    const listSources = vi.fn().mockResolvedValue([pod]);
    const api = { listSources } as unknown as Api;
    const view = render(<Probe api={api} polling />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listSources).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(listSources).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('n').textContent).toBe('1');
    view.rerender(<Probe api={api} polling={false} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const afterFinalize = listSources.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(listSources).toHaveBeenCalledTimes(afterFinalize);
  });
});
