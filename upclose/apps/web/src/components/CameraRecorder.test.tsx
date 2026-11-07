// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CameraRecorder } from './CameraRecorder';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('records one continuous source with audio disabled and stops at 180 seconds', async () => {
  vi.useFakeTimers();
  const stopTrack = vi.fn();
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] }));
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
  class Recorder {
    static isTypeSupported() { return true; }
    state = 'inactive'; mimeType = 'video/mp4';
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable?.({ data: new Blob(['synthetic recording'], { type: this.mimeType }) }); this.onstop?.(); }
  }
  vi.stubGlobal('MediaRecorder', Recorder);
  const recorded = vi.fn();
  render(<CameraRecorder partNo={0} maxSeconds={180} onRecorded={recorded} />);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open camera' })); });
  expect(getUserMedia).toHaveBeenCalledWith({ video: { facingMode: { ideal: 'environment' } }, audio: false });
  fireEvent.click(screen.getByRole('button', { name: 'Start recording' }));
  await act(async () => { vi.advanceTimersByTime(60000); });
  expect(recorded).not.toHaveBeenCalled();
  expect(screen.getByRole('timer').textContent).toContain('60s of up to 180s');
  await act(async () => { vi.advanceTimersByTime(120000); });
  expect(recorded).toHaveBeenCalledOnce();
  expect(recorded.mock.calls[0]?.[0].name).toBe('source-video.mp4');
  expect(recorded.mock.calls[0]?.[1]).toBe(180);
  expect(stopTrack).toHaveBeenCalled();
});
