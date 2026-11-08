import { useState, type ChangeEvent, type ReactElement } from 'react';
import { checkAudioFile, type AudioMeta, MAX_AUDIO_SECONDS, readAudioDuration, type AudioUploadController } from '../upload/audioUpload';
import { ErrorNotice } from './PartCard';
import { Button } from '../ui/Button';
import { Notice } from '../ui/Notice';

interface Props {
  upload: AudioUploadController;
  /** An engine_audio source is already attached to the inspection. */
  attached: boolean;
  capturing: boolean;
}

/** Pick or record an engine clip (WAV, MP3, M4A), check it locally, then send it. Mirrors the video upload controls. */
export function EngineAudioUpload({ upload, attached, capturing }: Props): ReactElement | null {
  const [rejection, setRejection] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [sample, setSample] = useState(false);

  if (attached) return <p className="hint" data-testid="audio-attached">Engine audio is attached.</p>;
  if (!capturing) return null;

  const pick = async (event: ChangeEvent<HTMLInputElement>, recorded: boolean): Promise<void> => {
    const file = event.target.files?.[0];
    event.target.value = ''; // allow choosing the same file again
    if (!file) return;
    setRejection(null);
    setNotice(null);
    setChecking(true);
    // Cheap checks first so an oversized or wrong-type file never waits on metadata.
    let verdict = checkAudioFile(file, null);
    if (verdict.ok) verdict = checkAudioFile(file, await readAudioDuration(file));
    setChecking(false);
    if (!verdict.ok) { setRejection(verdict.message); return; }
    setNotice(verdict.notice);
    // Recorded now is live, a chosen file is a replay (cached), and the sample box always wins with synthetic.
    const meta: AudioMeta = { provenance: sample ? 'synthetic' : recorded ? 'live' : 'cached', device: recorded ? 'phone-mic' : 'audio-file' };
    void upload.upload(file, verdict.contentType, meta);
  };

  const percent = upload.progress && upload.progress.totalBytes > 0 ? Math.round((upload.progress.uploadedBytes / upload.progress.totalBytes) * 100) : 0;
  const label = upload.phase === 'completing' ? 'Finishing upload...' : upload.phase === 'attaching' ? 'Attaching the clip...' : `Uploading engine audio... ${percent}%`;

  return (
    <div className="audio-upload stack" data-testid="engine-audio-upload">
      <p className="hint">WAV, MP3 or M4A, up to {MAX_AUDIO_SECONDS} s and 10 MiB.</p>
      {rejection && <div data-testid="audio-rejection"><Notice tone="error">{rejection}</Notice></div>}
      {notice && !upload.error && <p role="status" className="hint">{notice}</p>}
      {upload.error && <ErrorNotice error={upload.error} onDismiss={upload.dismissError} />}
      {checking && <p role="status" className="hint">Checking the clip...</p>}
      {upload.busy && (
        <div className="upload-status" role="status">
          <label className="upload-label">
            {label}
            <progress className="meter" max={100} value={percent} aria-label="Engine audio upload progress" data-testid="engine-audio-progress" />
          </label>
          <Button variant="secondary" size="sm" data-testid="engine-audio-cancel" onClick={() => { void upload.abort(); }}>Cancel upload</Button>
        </div>
      )}
      {upload.phase === 'attached' && <p role="status" className="hint">Attached. Checking the clip...</p>}
      {upload.phase === 'failed' && upload.canRetry && (
        <div className="cap-actions">
          <Button size="sm" data-testid="engine-audio-retry" onClick={() => { void upload.retry(); }}>Try again</Button>
          <Button variant="secondary" size="sm" onClick={() => { void upload.abort(); }}>Discard</Button>
        </div>
      )}
      {!upload.busy && upload.phase !== 'attached' && upload.phase !== 'failed' && (
        <label className="hint check-row">
          <input type="checkbox" data-testid="engine-audio-synthetic" checked={sample} onChange={(event) => setSample(event.target.checked)} />
          {' '}This is a sample or simulated clip
        </label>
      )}
      {!upload.busy && upload.phase !== 'attached' && upload.phase !== 'failed' && (
        <div className="cap-actions">
          <label className="btn btn-primary btn-sm file-btn">
            <span>Record engine audio</span>
            <input type="file" accept="audio/*" capture data-testid="engine-audio-record-input" className="sr-only" onChange={(event) => { void pick(event, true); }} />
          </label>
          <label className="btn btn-secondary btn-sm file-btn">
            <span>Choose audio file</span>
            <input type="file" accept="audio/wav,audio/x-wav,audio/mpeg,audio/mp4,audio/x-m4a,.wav,.mp3,.m4a" data-testid="engine-audio-file-input" className="sr-only" onChange={(event) => { void pick(event, false); }} />
          </label>
        </div>
      )}
      {upload.phase === 'failed' && !upload.canRetry && <p className="hint">Choose the clip again to retry.</p>}
    </div>
  );
}
