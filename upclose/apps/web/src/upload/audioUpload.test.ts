import { describe, expect, it } from 'vitest';
import { checkAudioFile, MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS, normalizeAudioType } from './audioUpload';

const clip = (over: Partial<{ type: string; name: string; size: number }> = {}) => ({ type: 'audio/wav', name: 'engine.wav', size: 1000, ...over });

describe('engine audio limits', () => {
  it('accepts WAV, MP3 and M4A by type or by extension', () => {
    expect(normalizeAudioType({ type: 'audio/wav' })).toBe('audio/wav');
    expect(normalizeAudioType({ type: 'audio/mpeg', name: 'a.mp3' })).toBe('audio/mpeg');
    expect(normalizeAudioType({ type: 'audio/x-m4a' })).toBe('audio/x-m4a');
    expect(normalizeAudioType({ type: '', name: 'Engine.M4A' })).toBe('audio/x-m4a');
    expect(normalizeAudioType({ type: 'audio/wav; codecs=1' })).toBe('audio/wav');
  });

  it('rejects other types, including a browser recording in WebM', () => {
    expect(normalizeAudioType({ type: 'audio/webm' })).toBeNull();
    expect(normalizeAudioType({ type: 'video/mp4', name: 'a.mp4' })).toBeNull();
    const verdict = checkAudioFile(clip({ type: 'audio/webm', name: 'rec.webm' }), 10);
    expect(verdict).toEqual({ ok: false, message: expect.stringContaining('WAV, MP3 or M4A') });
  });

  it('rejects an empty clip, a clip over 10 MiB and a clip over 120 s with a clear message', () => {
    expect(checkAudioFile(clip({ size: 0 }), 10)).toMatchObject({ ok: false, message: expect.stringContaining('empty') });
    expect(checkAudioFile(clip({ size: MAX_AUDIO_BYTES + 1 }), 10)).toMatchObject({ ok: false, message: expect.stringContaining('10 MiB') });
    expect(checkAudioFile(clip(), MAX_AUDIO_SECONDS + 5)).toMatchObject({ ok: false, message: expect.stringContaining('120 seconds') });
  });

  it('accepts the exact limits and notes an unreadable length instead of blocking', () => {
    expect(checkAudioFile(clip({ size: MAX_AUDIO_BYTES }), MAX_AUDIO_SECONDS)).toMatchObject({ ok: true, notice: null });
    expect(checkAudioFile(clip(), null)).toMatchObject({ ok: true, notice: expect.stringContaining('could not read the clip length') });
  });
});
