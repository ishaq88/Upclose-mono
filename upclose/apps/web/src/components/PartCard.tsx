import { useEffect, useMemo, useState, type ChangeEvent, type ReactElement } from 'react';
import type { PartState, PartSummary } from '../api/client';
import { formatBytes, normalizeVideoType } from '../capture/media';
import { qualityHints, readVideoMetadata, type QualityHint } from '../capture/quality';
import type { GuideStep } from '../capture/guide';
import { GuideViewer } from '../capture/GuideViewer';
import { QUALITY_WARNING_LINE, useVoiceGuide } from '../capture/voiceGuide';
import { MAX_CLIP_BYTES, MAX_RECORDING_SECONDS } from '../config';
import type { UiError } from '../errors';
import type { LocalUpload } from '../upload/useUploads';
import type { UploadContentType, UploadRecord } from '../upload/multipart';
import { describeReason } from '../progress/reasons';
import { CameraRecorder } from './CameraRecorder';
import '../capture/capture.css';

/** Plain-language server states. Nothing here is a client-invented status. */
export const STATE_LABEL: Record<PartState, string> = {
  pending: 'Not filmed yet',
  uploading: 'Uploading',
  uploaded: 'Uploaded, queued',
  processing: 'Processing',
  processed: 'Processed',
  failed: 'Failed',
  skipped: 'Skipped',
};

/** The state shown for a zone: the server's, except while this tab is actively sending its video. */
export function zoneStateLabel(state: PartState, local: LocalUpload | undefined): string {
  return local && local.phase !== 'failed' ? 'Uploading from this tab' : STATE_LABEL[state];
}

interface Draft {
  file: File;
  contentType: UploadContentType | null;
  hints: QualityHint[] | null;
  tooLarge: boolean;
  previewUrl: string | null;
}

export interface PartCardProps {
  guide: GuideStep;
  part: PartSummary;
  /** Shown as an "Optional" tag. Display only; the part number is unchanged. */
  optional: boolean;
  local: LocalUpload | undefined;
  record: UploadRecord | undefined;
  /** An in-memory copy of the file exists in this tab (so "Try again" needs no reselection). */
  hasFile: boolean;
  sessionCapturing: boolean;
  /** False when the page shows the 3D car itself (the laptop layout keeps one car beside the steps). */
  showViewer?: boolean;
  actionError: UiError | undefined;
  /** Jump to the next zone that still needs footage, when there is one. */
  next: { title: string; go: () => void } | null;
  onStart: (file: File, contentType: UploadContentType) => void;
  onRetry: () => void;
  onResumeWith: (file: File) => void;
  onRetake: () => void;
  /** Abort an attempt this tab has no record of, using the server-reported active upload id. */
  onDiscardStranded: () => void;
  onSkip: () => void;
  onDismissError: () => void;
}

export function PartCard(props: PartCardProps): ReactElement {
  const { guide, part, optional } = props;
  const headingId = `part-${part.part_no}-heading`;
  return (
    <article className="guide" aria-labelledby={headingId} data-part={part.part_no}>
      {props.showViewer !== false && <GuideViewer guide={guide} />}
      <div className="guide-body">
        <div className="guide-lead">
          <div className="guide-head">
            <h3 id={headingId}>{guide.title}{optional && <span className="opt-tag">Optional</span>}</h3>
            <span className="muted num">About {guide.targetSeconds} s</span>
          </div>
          <p className="guide-goal">{guide.goal}</p>
          {guide.prototypeNote && <p className="hint">{guide.prototypeNote}</p>}
        </div>
        <ol className="guide-steps">
          {guide.steps.map((step) => <li key={step}>{step}</li>)}
        </ol>
      </div>
      {part.state === 'failed' && part.reason && <p className="notice notice-error guide-note">{describeReason(part.reason)}</p>}
      {part.state === 'skipped' && <p className="notice guide-note">Skipped. This zone will show as not assessed.</p>}
      <Actions {...props} />
    </article>
  );
}

export function chipClass(state: PartState): string {
  switch (state) {
    case 'processed': return 'bg-teal-100 text-teal-900';
    case 'failed': return 'bg-red-100 text-red-900';
    case 'skipped': return 'bg-stone-200 text-stone-800';
    case 'uploading': return 'bg-sky-100 text-sky-900';
    case 'pending': return 'bg-stone-100 text-stone-800';
    default: return 'bg-amber-100 text-amber-900';
  }
}

function Actions(props: PartCardProps): ReactElement | null {
  const { part, local, record, sessionCapturing } = props;
  const busy = local !== undefined && local.phase !== 'failed';
  const accepted = !local && (part.state === 'uploaded' || part.state === 'processing' || part.state === 'processed');
  if (!sessionCapturing && part.state === 'pending' && !local) return null;
  return (
    <div className="guide-actions">
      {props.actionError && <ErrorNotice error={props.actionError} onDismiss={props.onDismissError} />}
      {local?.phase === 'failed' && local.error && <ErrorNotice error={local.error} onDismiss={props.onDismissError} />}
      {busy && <UploadProgress local={local} onCancel={props.onRetake} />}
      {local?.phase === 'failed' && <FailedControls {...props} />}
      {!local && part.state === 'pending' && <CaptureControls {...props} />}
      {!local && part.state === 'uploading' && <InterruptedControls {...props} record={record} />}
      {accepted && (
        <p className="hint">Clip accepted. To replace it, start a new session.</p>
      )}
      {props.next && part.state !== 'pending' && !busy && (
        <button type="button" className="btn btn-ghost btn-sm next-zone" onClick={props.next.go}>Next zone: {props.next.title}</button>
      )}
    </div>
  );
}

export function ErrorNotice({ error, onDismiss }: { error: UiError; onDismiss?: () => void }): ReactElement {
  return (
    <div role="alert" className="notice notice-error">
      <p className="notice-title">{error.title}</p>
      <p>{error.message}</p>
      {onDismiss && error.action !== 'new_session' && <button type="button" onClick={onDismiss} className="textlink">Dismiss</button>}
    </div>
  );
}

function UploadProgress({ local, onCancel }: { local: LocalUpload; onCancel: () => void }): ReactElement {
  const progress = local.progress;
  const percent = progress && progress.totalBytes > 0 ? Math.round((progress.uploadedBytes / progress.totalBytes) * 100) : 0;
  const label = local.phase === 'completing' ? 'Finishing upload...' : local.phase === 'preparing' ? 'Preparing upload...' : `Uploading... ${percent}%`;
  return (
    <div className="upload-status" role="status">
      <label className="upload-label">
        {label}
        <progress className="meter" max={100} value={percent} aria-label="Upload progress" />
      </label>
      <button type="button" onClick={onCancel} className="btn btn-ghost btn-sm">Cancel and retake</button>
    </div>
  );
}

function FailedControls(props: PartCardProps): ReactElement {
  const { record, hasFile } = props;
  return (
    <div className="stack">
      {hasFile ? (
        <button type="button" onClick={props.onRetry} className="btn">Try again</button>
      ) : record ? (
        <ReselectFile label="Select the same video to continue" onFile={props.onResumeWith} />
      ) : null}
      <div><button type="button" onClick={props.onRetake} className="textlink">Start this zone over</button></div>
    </div>
  );
}

function InterruptedControls({ record, ...props }: PartCardProps): ReactElement {
  if (!record) {
    return (
      <div className="notice notice-warn stack">
        <p>This zone has an upload this tab cannot continue. Discard it, then film or choose the video again.</p>
        {props.part.active_upload_id
          ? <button type="button" onClick={props.onDiscardStranded} className="btn btn-ghost btn-sm">Discard stranded upload</button>
          : <p>Nothing to discard; unfinished uploads close on their own after 24 hours.</p>}
      </div>
    );
  }
  return (
    <div className="notice notice-warn stack">
      <p>
        An upload for this zone was interrupted ({formatBytes(record.size)}{record.fileName ? `, ${record.fileName}` : ''}). Select the same video file again to continue; only missing pieces are sent. A clip recorded in the browser cannot be recovered, so use Start this zone over.
      </p>
      <ReselectFile label="Select the same video to continue" onFile={props.onResumeWith} />
      <div><button type="button" onClick={props.onRetake} className="textlink">Start this zone over</button></div>
    </div>
  );
}

function ReselectFile({ label, onFile }: { label: string; onFile: (file: File) => void }): ReactElement {
  return (
    <label className="btn btn-ghost btn-sm file-btn">
      {label}
      <input type="file" accept="video/*" className="sr-only" onChange={(event) => pickFile(event, onFile)} />
    </label>
  );
}

function pickFile(event: ChangeEvent<HTMLInputElement>, onFile: (file: File) => void): void {
  const file = event.target.files?.[0];
  event.target.value = ''; // allow choosing the same file again
  if (file) onFile(file);
}

function CaptureControls(props: PartCardProps): ReactElement {
  const { part, guide } = props;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [confirmSkip, setConfirmSkip] = useState(false);
  const name = guide.title.toLowerCase();
  const voice = useVoiceGuide();

  const accept = (file: File, recordedSeconds: number | null): void => {
    const tooLarge = file.size > MAX_CLIP_BYTES;
    const contentType = normalizeVideoType(file);
    const previewUrl = typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : null;
    setDraft({ file, contentType, hints: null, tooLarge, previewUrl });
    void readVideoMetadata(file).then((meta) => {
      const hints = qualityHints(meta, recordedSeconds);
      setDraft((current) => (current?.file === file ? { ...current, hints } : current));
      for (const hint of hints) {
        const line = QUALITY_WARNING_LINE[hint.id];
        if (line) voice.play(line, hint.message);
      }
    });
  };

  const discard = (): void => {
    setDraft((current) => {
      if (current?.previewUrl) URL.revokeObjectURL(current.previewUrl);
      return null;
    });
  };

  useEffect(() => () => { if (draft?.previewUrl) URL.revokeObjectURL(draft.previewUrl); }, [draft?.previewUrl]);

  const canUpload = useMemo(() => draft !== null && draft.contentType !== null && !draft.tooLarge, [draft]);

  if (draft) {
    return (
      <div className="review stack">
        <p className="review-title">Review your clip for {guide.title}</p>
        {draft.previewUrl && <video src={draft.previewUrl} controls muted playsInline className="review-video" aria-label="Preview of your clip" />}
        <p className="hint">{formatBytes(draft.file.size)}</p>
        {draft.tooLarge && <p role="alert" className="notice notice-error">This clip is larger than {MAX_CLIP_BYTES / (1024 * 1024)} MiB, which is the limit for one clip. Retake a shorter one.</p>}
        {draft.contentType === null && <p role="alert" className="notice notice-error">This file type is not supported. Use an MP4, MOV or WebM video.</p>}
        <HintList hints={draft.hints} />
        <div className="cap-actions review-actions">
          <button type="button" className="btn" disabled={!canUpload}
            onClick={() => { if (draft.contentType) { props.onStart(draft.file, draft.contentType); discard(); } }}>
            Upload this clip
          </button>
          <button type="button" className="btn btn-ghost" onClick={discard}>Retake</button>
        </div>
        <p className="hint">Hints are advice only; weak footage may be marked as not fully assessed.</p>
      </div>
    );
  }

  return (
    <>
      <div className="thumbbar">
        {confirmSkip ? (
          <div className="skip-confirm">
            <p>Skip {guide.title}? This cannot be undone, and the zone will show as not assessed.</p>
            <div className="cap-actions">
              <button type="button" className="btn btn-dark" onClick={() => { setConfirmSkip(false); props.onSkip(); }}>Yes, skip {guide.title}</button>
              <button type="button" className="btn btn-ghost" onClick={() => setConfirmSkip(false)}>Keep it</button>
            </div>
          </div>
        ) : (
          <div className="cap-actions">
            <label className="btn primary hide-lg file-btn">
              <RecordIcon />
              <span>Record {name}</span>
              <input type="file" accept="video/*" capture="environment" className="sr-only" onChange={(event) => pickFile(event, (file) => accept(file, null))} />
            </label>
            <label className="btn btn-up file-btn">
              <UploadIcon />
              <span>Upload<span className="hide-sm"> {name} clip</span></span>
              <input type="file" accept="video/*" className="sr-only" onChange={(event) => pickFile(event, (file) => accept(file, null))} />
            </label>
            <button type="button" className="textlink" onClick={() => setConfirmSkip(true)}>Skip this zone</button>
          </div>
        )}
      </div>
      <div className="webcam hide-sm">
        <p className="webcam-title">Or record with your webcam</p>
        <CameraRecorder partNo={part.part_no} maxSeconds={MAX_RECORDING_SECONDS} onRecorded={(file, seconds) => accept(file, seconds)} />
      </div>
    </>
  );
}

function RecordIcon(): ReactElement {
  return <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="8" fill="#111315" /><circle cx="9" cy="9" r="4" fill="#ff5a1f" /></svg>;
}

function UploadIcon(): ReactElement {
  return <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M9 12V2M5 6l4-4 4 4M2 15h14" /></svg>;
}

function HintList({ hints }: { hints: QualityHint[] | null }): ReactElement {
  if (hints === null) return <p className="hint" role="status">Checking video details...</p>;
  if (hints.length === 0) return <p className="hint">No length or size problems. Blur and light are checked after upload.</p>;
  return (
    <ul className="hints" aria-label="Quality hints">
      {hints.map((hint) => (
        <li key={hint.id} className={hint.severity === 'warn' ? 'notice notice-warn' : 'notice'}>{hint.message}</li>
      ))}
    </ul>
  );
}
