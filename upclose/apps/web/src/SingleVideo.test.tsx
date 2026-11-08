// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { FakeBackend } from './testing/fakeBackend';

vi.mock('./capture/Guide3D', () => ({ default: () => null }));

let backend: FakeBackend;
beforeEach(() => { sessionStorage.clear(); backend = new FakeBackend(); vi.stubGlobal('fetch', backend.fetch); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); });
const file = (value = 'source footage') => new File([value], 'walkaround.mp4', { type: 'video/mp4' });
function seed() {
  backend.captureMode = 'single_video';
  sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
}
async function selectVideo(value = file()) {
  fireEvent.change(await screen.findByLabelText('Choose one video from this device'), { target: { files: [value] } });
  return screen.findByRole('button', { name: 'Upload this video' });
}
const finish = () => screen.getByRole('button', { name: 'Finish capture' }) as HTMLButtonElement;

describe('flexible capture', () => {
  it('creates the selected mode and reloads it from server state', async () => {
    const view = render(<App />);
    fireEvent.click(screen.getByRole('radio', { name: /One video/ }));
    fireEvent.click(screen.getByLabelText(/I understand and agree/));
    fireEvent.click(screen.getByRole('button', { name: 'Continue with one video' }));
    await screen.findByRole('heading', { name: 'One video' });
    expect(backend.callsTo('POST', '/sessions')[0]?.body).toMatchObject({ capture_mode: 'single_video' });
    view.unmount(); render(<App />);
    await screen.findByRole('heading', { name: 'One video' });
    expect(screen.queryByText(/not connected/)).toBeNull();
    expect(screen.queryByText(/Findings are synthetic test data/)).toBeNull();
    expect((await screen.findByLabelText('Record one video with phone camera')).getAttribute('capture')).toBe('environment');
  });

  it('uploads a single source without hints and blocks Finish until server processing settles', async () => {
    seed(); render(<App />);
    fireEvent.click(await selectVideo());
    await screen.findByText(/Video accepted/);
    expect(backend.callsTo('POST', `/sessions/${backend.sessionId}/video`)[0]?.body).toMatchObject({ segments: [] });
    expect(backend.callsTo('POST', '/parts')).toHaveLength(0);
    expect(finish().disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Upload this video' })).toBeNull();
    backend.source!.processing_state = 'ready';
    await screen.findByText(/Source preparation complete/, {}, { timeout: 3500 });
    expect(finish().disabled).toBe(false);
    expect(screen.getByText(/Add a clip for any area the video missed/)).toBeTruthy();
    fireEvent.click(finish());
    fireEvent.click(screen.getByRole('button', { name: 'Yes, finish' }));
    await screen.findByText(/Capture is finished/);
    expect(backend.parts.every((part) => part.state === 'skipped')).toBe(true);
  });

  it('rejects overlapping hints before sending and uploads a corrected partial mapping', async () => {
    seed(); render(<App />); await selectVideo();
    fireEvent.click(screen.getByRole('button', { name: 'Add a marked section' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add a marked section' }));
    fireEvent.change(screen.getByLabelText('Start seconds 1'), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText('End seconds 1'), { target: { value: '20' } });
    fireEvent.change(screen.getByLabelText('Start seconds 2'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('End seconds 2'), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: 'Upload this video' }));
    expect(screen.getByRole('alert').textContent).toContain('overlap');
    expect(backend.source).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Remove section 2' }));
    fireEvent.click(screen.getByRole('button', { name: 'Upload this video' }));
    await screen.findByText(/Video accepted/);
    expect(backend.source!.segments).toEqual([{ part_no: 1, start_ms: 0, end_ms: 20000 }]);
  });

  it('resumes after reload, rejects a different file, and cancels an unfinished upload', async () => {
    seed(); backend.holdPuts = true;
    const view = render(<App />);
    fireEvent.click(await selectVideo());
    await waitFor(() => expect(sessionStorage.getItem(`upclose.source.v1.${backend.sessionId}`)).not.toBeNull());
    expect(sessionStorage.getItem(`upclose.uploads.v1.${backend.sessionId}`)).toBeNull();
    view.unmount();
    backend.holdPuts = false;
    render(<App />);
    const input = await screen.findByLabelText('Select the same source video to resume');
    fireEvent.change(input, { target: { files: [file('wrong')] } });
    fireEvent.click(await screen.findByRole('button', { name: 'Resume source upload' }));
    await screen.findByText(/different size/);
    expect(backend.source!.state).toBe('open');
    fireEvent.click(screen.getByRole('button', { name: 'Discard unfinished source upload' }));
    await screen.findByLabelText('Choose one video from this device');
    expect(backend.source!.state).toBe('aborted');
    expect(finish().disabled).toBe(false);
  });

  it('shows source failure as unassessed and permits a guided follow-up', async () => {
    seed(); render(<App />);
    fireEvent.click(await selectVideo());
    await screen.findByText(/Video accepted/);
    backend.source!.processing_state = 'failed';
    await screen.findByText(/Source preparation failed/, {}, { timeout: 3500 });
    expect(screen.getByText(/could not be prepared/)).toBeTruthy();
    expect(finish().disabled).toBe(false);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Zones' })).getByRole('button', { name: /^Passenger side/ }));
    const card = screen.getByRole('heading', { level: 3, name: /^Passenger side/ }).closest('article')!;
    fireEvent.change(within(card).getByLabelText(/Upload passenger side clip/), { target: { files: [file()] } });
    fireEvent.click(await within(card).findByRole('button', { name: 'Upload this clip' }));
    await waitFor(() => expect(backend.parts[1]!.state).toBe('uploaded'));
    expect(backend.callsTo('POST', '/parts')[0]?.body).toMatchObject({ part_no: 2 });
  });

  it('rejects an oversize file before creating an upload', async () => {
    seed(); render(<App />);
    const oversized = file();
    Object.defineProperty(oversized, 'size', { value: 200 * 1024 * 1024 + 1 });
    const button = await selectVideo(oversized);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('alert').textContent).toContain('200 MiB');
    expect(backend.source).toBeNull();
  });

  it('discovers a stranded source without local metadata and allows discarding it', async () => {
    seed();
    await backend.fetch(`/api/v1/sessions/${backend.sessionId}/video`, { method: 'POST', headers: { Authorization: `Bearer ${backend.token}` }, body: JSON.stringify({ content_type: 'video/mp4', content_length: 20, segments: [] }) });
    render(<App />);
    await screen.findByText(/Unfinished upload/);
    expect(finish().disabled).toBe(true);
    expect(screen.queryByLabelText('Choose one video from this device')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Discard unfinished source upload' }));
    await screen.findByLabelText('Choose one video from this device');
    expect(backend.source!.state).toBe('aborted');
  });
});
