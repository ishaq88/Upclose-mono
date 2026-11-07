import { useCallback, useEffect, useRef, useState, type ReactElement } from 'react';
import '../capture/capture.css';
import { classifyCameraError, detectCameraSupport, pickRecorderMimeType, type CameraFailure } from '../capture/media';

type CameraState =
  | { kind: 'idle' }
  | { kind: 'starting' }
  | { kind: 'live' }
  | { kind: 'recording'; seconds: number }
  | { kind: 'failed'; reason: CameraFailure };

interface Props {
  partNo: number;
  maxSeconds: number;
  onRecorded: (file: File, recordedSeconds: number) => void;
}

const FAILURE_COPY: Record<CameraFailure, string> = {
  denied: 'Camera access is blocked for this site. Allow Camera in your browser or phone settings for this site, then press "Open camera" again. You can also use the options above to record with your phone\'s own camera app or pick a video.',
  no_camera: 'No usable camera was found. Use the options above to choose a video instead.',
  busy: 'The camera is in use by another app or tab. Close it and try again, or use the options above.',
  other: 'The camera could not be started. Try again, or use the options above to choose a video.',
};

/** In-page recording with MediaRecorder; the part-specific file inputs are the always-available fallback. */
export function CameraRecorder({ partNo, maxSeconds, onRecorded }: Props): ReactElement {
  const [state, setState] = useState<CameraState>({ kind: 'idle' });
  const support = detectCameraSupport();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const startedAt = useRef(0);
  const discard = useRef(false);

  const releaseCamera = useCallback(() => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  useEffect(() => {
    discard.current = false; // re-mounted (StrictMode) after a simulated unmount
    return () => {
      discard.current = true;
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
      releaseCamera();
    };
  }, [releaseCamera]);

  const openCamera = async (): Promise<void> => {
    setState({ kind: 'starting' });
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
      if (discard.current) { stream.getTracks().forEach((track) => track.stop()); return; }
      streamRef.current = stream;
      setState({ kind: 'live' });
      requestAnimationFrame(() => {
        if (videoRef.current) videoRef.current.srcObject = stream;
      });
    } catch (error) {
      setState({ kind: 'failed', reason: classifyCameraError(error) });
    }
  };

  const closeCamera = (): void => {
    releaseCamera();
    setState({ kind: 'idle' });
  };

  const finishRecording = (mime: string): void => {
    const seconds = (Date.now() - startedAt.current) / 1000;
    const type = recorderRef.current?.mimeType || mime;
    const blob = new Blob(chunks.current, { type });
    chunks.current = [];
    recorderRef.current = null;
    releaseCamera();
    if (discard.current) return;
    setState({ kind: 'idle' });
    if (blob.size === 0) {
      setState({ kind: 'failed', reason: 'other' });
      return;
    }
    const extension = type.includes('mp4') ? 'mp4' : 'webm';
    onRecorded(new File([blob], `${partNo === 0 ? 'source-video' : `part-${partNo}`}.${extension}`, { type, lastModified: Date.now() }), seconds);
  };

  const startRecording = (): void => {
    const stream = streamRef.current;
    if (!stream) return;
    const requested = pickRecorderMimeType(window.MediaRecorder);
    let recorder: MediaRecorder;
    try {
      recorder = requested ? new MediaRecorder(stream, { mimeType: requested }) : new MediaRecorder(stream);
    } catch {
      setState({ kind: 'failed', reason: 'other' });
      releaseCamera();
      return;
    }
    chunks.current = [];
    recorderRef.current = recorder;
    recorder.ondataavailable = (event: BlobEvent) => { if (event.data.size > 0) chunks.current.push(event.data); };
    recorder.onstop = () => finishRecording(requested ?? 'video/webm');
    startedAt.current = Date.now();
    recorder.start(1000);
    setState({ kind: 'recording', seconds: 0 });
    timer.current = setInterval(() => {
      const elapsed = Math.floor((Date.now() - startedAt.current) / 1000);
      setState({ kind: 'recording', seconds: elapsed });
      if (elapsed >= maxSeconds && recorder.state === 'recording') recorder.stop();
    }, 500);
  };

  const stopRecording = (): void => {
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
  };

  if (support !== 'available') {
    return (
      <p className="notice cam-note">
        {support === 'insecure_context'
          ? 'Recording inside this page needs a secure (HTTPS) connection. You can still film with your phone\'s camera app using the options above.'
          : 'This browser cannot record video inside the page. Use the options above to film with your camera app or choose a video.'}
      </p>
    );
  }

  const live = state.kind === 'live' || state.kind === 'recording';
  return (
    <div className="cam">
      {state.kind === 'failed' && (
        <p role="alert" className="notice notice-warn">{FAILURE_COPY[state.reason]}</p>
      )}
      {live && (
        <video ref={videoRef} autoPlay muted playsInline aria-label={`Live camera preview for ${partNo === 0 ? 'one continuous video' : partNo === 7 ? 'the low underbody sweep' : `part ${partNo}`}`}
          className="cam-preview" />
      )}
      <div className="cam-row">
        {(state.kind === 'idle' || state.kind === 'failed') && (
          <button type="button" onClick={() => { void openCamera(); }} className="btn btn-ghost btn-sm">Open camera</button>
        )}
        {state.kind === 'starting' && <span className="hint" role="status">Waiting for camera permission...</span>}
        {state.kind === 'live' && (
          <>
            <button type="button" onClick={startRecording} className="btn btn-sm">Start recording</button>
            <button type="button" onClick={closeCamera} className="textlink">Close camera</button>
          </>
        )}
        {state.kind === 'recording' && (
          <>
            <button type="button" onClick={stopRecording} className="btn btn-sm btn-rec">Stop recording</button>
            <span role="timer" aria-live="off" className="hint num">
              {state.seconds}s of up to {maxSeconds}s
            </span>
          </>
        )}
      </div>
    </div>
  );
}
