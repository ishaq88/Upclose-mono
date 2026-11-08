import { useState, type ChangeEvent, type ReactElement } from 'react';
import { checkAudioFile, MAX_AUDIO_SECONDS, readAudioDuration, type AudioUploadController } from '../upload/audioUpload';
import { ErrorNotice } from './PartCard';

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

  if (attached) return <p className="hint" data-testid="audio-attached">Engine audio is attached to this inspection.</p>;
  if (!capturing) return null;

  const pick = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
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
    void upload.upload(file, verdict.contentType);
  };

  const percent = upload.progress && upload.progress.totalBytes > 0 ? Math.round((upload.progress.uploadedBytes / upload.progress.totalBytes) * 100) : 0;
  const label = upload.phase === 'completing' ? 'Finishing upload...' : `Uploading engine audio... ${percent}%`;

  return (
    <div className="audio-upload stack" data-testid="engine-audio-upload">
      <p className="hint">Add a WAV, MP3 or M4A clip of the engine, up to {MAX_AUDIO_SECONDS} seconds and 10 MiB. Not attached shows as Not checked.</p>
      {rejection && <p role="alert" className="notice notice-error" data-testid="audio-rejection">{rejection}</p>}
      {notice && !upload.error && <p role="status" className="hint">{notice}</p>}
      {upload.error && <ErrorNotice error={upload.error} onDismiss={upload.dismissError} />}
      {checking && <p role="status" className="hint">Checking the clip...</p>}
      {upload.busy && (
        <div className="upload-status" role="status">
          <label className="upload-label">
            {label}
            <progress className="meter" max={100} value={percent} aria-label="Engine audio upload progress" data-testid="engine-audio-progress" />
          </label>
          <button type="button" className="btn btn-ghost btn-sm" data-testid="engine-audio-cancel" onClick={() => { void upload.abort(); }}>Cancel upload</button>
        </div>
      )}
      {upload.phase === 'accepted' && <p role="status" className="hint">Uploaded. Waiting for the clip to be checked and attached.</p>}
      {upload.phase === 'failed' && upload.canRetry && (
        <div className="cap-actions">
          <button type="button" className="btn btn-sm" data-testid="engine-audio-retry" onClick={() => { void upload.retry(); }}>Try again</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void upload.abort(); }}>Discard</button>
        </div>
      )}
      {!upload.busy && upload.phase !== 'accepted' && upload.phase !== 'failed' && (
        <div className="cap-actions">
          <label className="btn btn-sm file-btn">
            <span>Record engine audio</span>
            <input type="file" accept="audio/*" capture data-testid="engine-audio-record-input" className="sr-only" onChange={(event) => { void pick(event); }} />
          </label>
          <label className="btn btn-ghost btn-sm file-btn">
            <span>Choose audio file</span>
            <input type="file" accept="audio/wav,audio/x-wav,audio/mpeg,audio/mp4,audio/x-m4a,.wav,.mp3,.m4a" data-testid="engine-audio-file-input" className="sr-only" onChange={(event) => { void pick(event); }} />
          </label>
        </div>
      )}
      {upload.phase === 'failed' && !upload.canRetry && <p className="hint">Choose the clip again to retry.</p>}
    </div>
  );
}
