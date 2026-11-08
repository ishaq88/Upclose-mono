// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { FakeBackend } from '../testing/fakeBackend';

vi.mock('./Guide3D', () => ({ default: () => null }));
const durations = vi.hoisted(() => ({ seconds: 15 as number | null }));
vi.mock('../upload/audioUpload', async (original) => ({
  ...(await original<typeof import('../upload/audioUpload')>()),
  readAudioDuration: async () => durations.seconds,
}));

let backend: FakeBackend;
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  durations.seconds = 15;
  backend = new FakeBackend();
  backend.sessionState = 'capturing';
  vi.stubGlobal('fetch', backend.fetch);
  sessionStorage.setItem('upclose.session.v1', JSON.stringify({ sessionId: backend.sessionId, token: backend.token }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

const pick = async (file: File) => {
  fireEvent.change(await screen.findByLabelText('Choose audio file'), { target: { files: [file] } });
};
const wav = (bytes = 2000, name = 'engine.wav', type = 'audio/wav') => new File([new Uint8Array(bytes)], name, { type });
const audioCalls = (method: string, part: string) => backend.callsTo(method, part);

describe('engine audio upload', () => {
  it('creates, presigns, PUTs, completes, then the source shows as attached', async () => {
    render(<App />);
    await pick(wav());
    await screen.findByTestId('audio-attached');
    expect(audioCalls('POST', `/sessions/${backend.sessionId}/audio`)).toHaveLength(1);
    expect(audioCalls('POST', '/urls')).toHaveLength(1);
    expect(backend.calls.filter((call) => call.method === 'PUT' && call.url.startsWith('https://storage.test/audio-'))).toHaveLength(1);
    expect(audioCalls('POST', '/complete')).toHaveLength(1);
    expect(backend.calls.find((call) => call.url.endsWith('/audio'))?.body).toEqual({ content_type: 'audio/wav', content_length: 2000 });
    await waitFor(() => expect(screen.getByTestId('kit-engine_audio').textContent).toContain('Attached'));
  });

  it('rejects a wrong type, an oversized clip and a clip over 120 s before any request', async () => {
    render(<App />);
    await pick(wav(100, 'rec.webm', 'audio/webm'));
    expect((await screen.findByTestId('audio-rejection')).textContent).toContain('WAV, MP3 or M4A');
    const big = wav(10);
    Object.defineProperty(big, 'size', { value: 10 * 1024 * 1024 + 1 });
    await pick(big);
    await waitFor(() => expect(screen.getByTestId('audio-rejection').textContent).toContain('10 MiB'));
    durations.seconds = 121;
    await pick(wav());
    await waitFor(() => expect(screen.getByTestId('audio-rejection').textContent).toContain('120 seconds'));
    expect(audioCalls('POST', '/audio')).toHaveLength(0);
  });

  it('shows a server refusal, and Try again then succeeds', async () => {
    backend.audioCreateScript = [500];
    render(<App />);
    await pick(wav());
    expect(await screen.findByRole('alert')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByTestId('audio-attached');
    expect(audioCalls('POST', `/sessions/${backend.sessionId}/audio`)).toHaveLength(2);
  });

  it('cancels an upload in flight and aborts the server attempt', async () => {
    backend.holdPuts = true;
    render(<App />);
    await pick(wav());
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel upload' }));
    await waitFor(() => expect(audioCalls('DELETE', '/audio/')).toHaveLength(1));
    await screen.findByLabelText('Choose audio file');
    expect(audioCalls('POST', '/complete')).toHaveLength(0);
  });
  const sourcePosts = () => backend.calls.filter((call) => call.method === 'POST' && call.url.endsWith(`/sessions/${backend.sessionId}/sources`));

  it('posts the engine_audio source with the upload id and sha256, labeled cached for a chosen file', async () => {
    render(<App />);
    await pick(wav());
    await screen.findByTestId('audio-attached');
    expect(sourcePosts()).toHaveLength(1);
    const body = sourcePosts()[0]!.body as Record<string, unknown> & { data: { upload_id: string; sha256: string } };
    expect(body).toMatchObject({ kind: 'engine_audio', provenance: 'cached', device: 'audio-file' });
    expect(body.data.upload_id).toMatch(/^audio-/);
    expect(body.data.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(Number.isNaN(Date.parse(String(body.captured_at)))).toBe(false);
  });

  it('labels a clip recorded in the app live, and a sample clip synthetic', async () => {
    render(<App />);
    fireEvent.change(await screen.findByLabelText('Record engine audio'), { target: { files: [wav()] } });
    await screen.findByTestId('audio-attached');
    expect(sourcePosts()[0]!.body).toMatchObject({ provenance: 'live', device: 'phone-mic' });
    cleanup();

    backend.sources = [];
    render(<App />);
    fireEvent.click(await screen.findByTestId('engine-audio-synthetic'));
    await pick(wav());
    await waitFor(() => expect(sourcePosts()).toHaveLength(2));
    expect(sourcePosts()[1]!.body).toMatchObject({ provenance: 'synthetic', device: 'audio-file' });
  });

  it('does not show Attached until the source POST succeeds, and retries only the attach', async () => {
    backend.sourcePostScript = [500];
    render(<App />);
    await pick(wav());
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByTestId('audio-attached')).toBeNull();
    expect(screen.getByTestId('kit-engine_audio').textContent).not.toContain('Attached');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByTestId('audio-attached');
    expect(sourcePosts()).toHaveLength(2);
    expect(audioCalls('POST', `/sessions/${backend.sessionId}/audio`)).toHaveLength(1);
    expect(audioCalls('POST', '/complete')).toHaveLength(1);
  });

  it('treats 409 as already attached and re-reads the sources; a 404 or 401 is an error', async () => {
    backend.sourcePostScript = [409];
    render(<App />);
    await pick(wav());
    await waitFor(() => expect(sourcePosts()).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    cleanup();

    backend.sourcePostScript = [404];
    render(<App />);
    await pick(wav());
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByTestId('audio-attached')).toBeNull();
  });
});
