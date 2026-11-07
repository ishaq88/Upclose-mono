import { useEffect, useState, type ChangeEvent, type ReactElement } from 'react';
import { GUIDE_STEPS } from '../capture/guide';
import { formatBytes, normalizeVideoType } from '../capture/media';
import { readVideoMetadata } from '../capture/quality';
import { parseVideoRanges, type RangeDraft } from '../capture/videoRanges';
import { MAX_CLIP_BYTES } from '../config';
import type { SourceUploadController } from '../upload/useSourceUpload';
import { CameraRecorder } from './CameraRecorder';
import { ErrorNotice } from './PartCard';
import '../capture/capture.css';

export function SingleVideoCapture({ upload, capturing }: { upload: SourceUploadController; capturing: boolean }): ReactElement {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [ranges, setRanges] = useState<RangeDraft[]>([]);
  const [validation, setValidation] = useState<string | null>(null);
  const { source, busy, record, progress } = upload;
  const type = file ? normalizeVideoType(file) : null;
  const accepted = source?.state === 'accepted';
  const unfinished = source?.state === 'open' || source?.state === 'completing' || record !== null;

  useEffect(() => {
    if (!file) { setPreview(null); return; }
    const url = typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : null;
    setPreview(url);
    let active = true;
    void readVideoMetadata(file).then((meta) => { if (active && meta.durationSeconds !== null) setDuration(meta.durationSeconds); });
    return () => { active = false; if (url) URL.revokeObjectURL(url); };
  }, [file]);

  const select = (value: File, seconds: number | null = null) => { setFile(value); setDuration(seconds); setValidation(null); };
  const send = () => {
    if (!file || !type) return;
    try {
      if (file.size === 0 || file.size > MAX_CLIP_BYTES) throw new Error('Choose a nonempty video up to 200 MiB.');
      if (duration !== null && duration > 180) throw new Error('This video exceeds the 180-second limit. Choose a shorter clip.');
      const segments = record ? [] : parseVideoRanges(ranges, duration);
      setValidation(null);
      void upload.upload(file, type, segments);
    } catch (caught) { setValidation(caught instanceof Error ? caught.message : 'Check the marked sections.'); }
  };


  const picking = capturing && !accepted && source !== undefined && !busy;
  const choosing = picking && (!unfinished || record !== null);
  const setRange = (index: number, patch: Partial<RangeDraft>): void => setRanges((items) => items.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  const pickInto = (event: ChangeEvent<HTMLInputElement>): void => {
    const value = event.target.files?.[0];
    event.target.value = '';
    if (value) select(value);
  };

  return <section aria-labelledby="single-video-heading" className="svc">
    <div className="svc-head">
      <h2 id="single-video-heading" className="page-title">One video</h2>
      <p className="muted">Record a continuous walkaround or upload a partial clip. MP4, MOV or WebM, up to 180 seconds and 200 MiB. A 30–60 second clip is welcome.</p>
    </div>
    <p className="notice notice-warn">Automatic area recognition is not connected — mark sections yourself or use guided clips. Your report labels whether findings are synthetic test data or real model output. Unmapped areas remain unassessed; no missing area is treated as damage-free.</p>
    <div className="prog">
      <div className="stack">
        <div className="card stack">
          {upload.error && <ErrorNotice error={upload.error} />}
          {validation && <p role="alert" className="notice notice-error">{validation}</p>}
          {source === undefined && <p role="status">Checking source video status…</p>}
          {accepted && <>
            <p role="status" className="svc-status">Video accepted. {source.processing_state === 'ready' ? 'Source preparation complete.' : source.processing_state === 'failed' ? 'Source preparation failed.' : 'Preparing source video…'}</p>
            {source.processing_state === 'failed' && <p className="notice notice-error">This source could not be prepared. Its missing areas are not assessed. Add guided follow-up clips below or finish with missing coverage. Use a new session to replace the source.</p>}
            {source.processing_state === 'ready' && <p>The video is stored, but this does not verify coverage. Marked ranges are user-declared hints only. Add a guided clip for any available missing area below, or Finish with missing coverage.</p>}
            <p className="hint">An accepted source and its marked sections cannot be replaced in this session.</p>
          </>}
          {busy && <div role="status" className="stack"><p className="svc-status">{progress?.phase === 'completing' ? 'Confirming video upload with the server…' : 'Uploading source video…'}</p><progress aria-label="Source upload progress" className="meter" max={progress?.totalBytes ?? 1} value={progress?.uploadedBytes ?? 0} /><p className="hint">Keep this tab open until the upload finishes.</p></div>}
          {picking && unfinished && <p className="notice notice-warn">An unfinished source upload exists. {record ? 'Select the same video to resume only missing pieces. Browser recordings are lost after reload.' : 'This tab has no resume details. Discard the unfinished upload before starting again.'}</p>}
          {choosing && <>
            <div className="cap-actions svc-pick">
              {!unfinished && <label className="btn file-btn"><RecordIcon />Record one video with phone camera<input type="file" accept="video/*" capture="environment" className="sr-only" onChange={pickInto} /></label>}
              <label className={unfinished ? 'btn file-btn' : 'btn btn-ghost file-btn'}>{unfinished ? 'Select the same source video to resume' : 'Choose one video from this device'}<input type="file" accept="video/*" className="sr-only" onChange={pickInto} /></label>
            </div>
            {!unfinished && !file && <div className="webcam"><p className="webcam-title">Or record with your webcam</p><CameraRecorder partNo={0} maxSeconds={180} onRecorded={(value, seconds) => select(value, seconds)} /></div>}
            {file && <div className="svc-file stack">
              {preview && <video aria-label="Preview of your source video" src={preview} controls muted playsInline className="review-video" />}
              <p className="svc-meta"><b>{file.name}</b><span className="muted num">{formatBytes(file.size)}{duration !== null ? ` · ${duration.toFixed(1)} seconds` : ' · Duration will be checked by the server'}</span></p>
              {!type && <p role="alert" className="notice notice-error">Choose an MP4, MOV or WebM video.</p>}
              {file.size > MAX_CLIP_BYTES && <p role="alert" className="notice notice-error">This video exceeds the 200 MiB limit.</p>}
              {duration !== null && duration > 180 && <p role="alert" className="notice notice-error">This video exceeds the 180-second limit.</p>}
            </div>}
          </>}
          {capturing && !accepted && (unfinished || busy) && <div><button type="button" className="textlink" onClick={() => { void upload.abort(); }}>Discard unfinished source upload</button></div>}
        </div>
      </div>
      {choosing && file && <div className="stack">
        {!unfinished && <fieldset className="marks stack">
          <legend>Mark where each zone starts <span className="optional">(optional)</span></legend>
          <p className="muted">You can upload without marking anything. These are your hints, not verified coverage. Use seconds from the start of the video; one section per area, at most 60 seconds each, without overlaps.</p>
          {ranges.map((range, index) => <div key={index} className="mark-row">
            <label>Area {index + 1}<select value={range.partNo} onChange={(event) => setRange(index, { partNo: Number(event.target.value) })}>{GUIDE_STEPS.map((guide) => <option key={guide.partNo} value={guide.partNo}>{guide.title}</option>)}</select></label>
            <label>Start seconds {index + 1}<input type="number" min="0" max="180" step="0.001" value={range.start} onChange={(event) => setRange(index, { start: event.target.value })} /></label>
            <label>End seconds {index + 1}<input type="number" min="0" max="180" step="0.001" value={range.end} onChange={(event) => setRange(index, { end: event.target.value })} /></label>
            <button type="button" className="textlink" aria-label={`Remove section ${index + 1}`} onClick={() => setRanges((items) => items.filter((_, i) => i !== index))}>Remove</button>
          </div>)}
          <div><button type="button" className="btn btn-ghost btn-sm" disabled={ranges.length >= 8} onClick={() => setRanges((items) => [...items, { partNo: GUIDE_STEPS.find((guide) => !items.some((item) => item.partNo === guide.partNo))?.partNo ?? 1, start: '', end: '' }])}>Add a marked section</button></div>
        </fieldset>}
        <div className="thumbbar">
          <button type="button" className="btn btn-block" disabled={!type || file.size === 0 || file.size > MAX_CLIP_BYTES || (duration !== null && duration > 180)} onClick={send}>{record ? 'Resume source upload' : 'Upload this video'}</button>
        </div>
      </div>}
    </div>
    <details className="svc-guide"><summary>Optional filming guide</summary>
      <p>Move slowly in good light, show wide views then details, and avoid people, plates and documents. You can follow this guide in one continuous recording or show just the areas available to you.</p>
      <ul>{GUIDE_STEPS.map((guide) => <li key={guide.partNo}><strong>{guide.title}: </strong>{guide.goal}{guide.partNo === 7 && ' Stay standing outside the car. Do not crawl, kneel under, lie down, or reach beneath the vehicle.'}</li>)}</ul>
    </details>
  </section>;
}

function RecordIcon(): ReactElement {
  return <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="8" fill="#111315" /><circle cx="9" cy="9" r="4" fill="#ff5a1f" /></svg>;
}
