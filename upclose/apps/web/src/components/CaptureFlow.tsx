import { useCallback, useMemo, useState, type ReactElement } from 'react';
import type { Api, PartSummary } from '../api/client';
import { SaveInspection } from '../accounts/SaveInspection';
import { GUIDE_STEPS, guideFor } from '../capture/guide';
import { OPTIONAL_PARTS, sortForDisplay } from '../capture/zones';
import { normalizeVideoType } from '../capture/media';
import { describeError, type UiError } from '../errors';
import { useSession } from '../session/useSession';
import { useSourceUpload } from '../upload/useSourceUpload';
import { SingleVideoCapture } from './SingleVideoCapture';
import { useUploads } from '../upload/useUploads';
import { ErrorNotice, PartCard, STATE_LABEL, zoneStateLabel } from './PartCard';
import { Part1Slot } from './FreeVerdictCard';
import { ProgressPanel } from './ProgressPanel';
import { CaptureKits, MissingKitsNotice } from './CaptureKits';
import { kitRows, missingKits } from '../capture/kits';
import { useCaptureSources } from '../capture/useCaptureSources';
import { useAudioUpload } from '../upload/audioUpload';
import { useVoiceGuide, VoiceGuideBar, VoiceGuideProvider, zoneLineId } from '../capture/voiceGuide';
import { EngineCheck } from './EngineCheck';
import '../capture/capture.css';

interface Props {
  api: Api;
  sessionId: string;
  /** The inspection's owner token, passed only to SaveInspection so it can link the inspection to an account. */
  ownerToken: string;
  /** Forget the stored session so the consent screen shows again. */
  onNewSession: () => void;
}

export function CaptureFlow(props: Props): ReactElement {
  return <VoiceGuideProvider api={props.api}><CaptureFlowBody {...props} /></VoiceGuideProvider>;
}

function CaptureFlowBody({ api, sessionId, ownerToken, onNewSession }: Props): ReactElement {
  const voice = useVoiceGuide();
  const { session, progress, error, unauthorized, refresh, markUnauthorized } = useSession(api, sessionId);
  const singleVideo = session?.capture_mode === 'single_video';
  const sourceUpload = useSourceUpload({ api, sessionId, enabled: singleVideo && session?.state !== 'deleting' && session?.state !== 'deleted' && !unauthorized, onChanged: refresh, onUnauthorized: markUnauthorized });
  const uploads = useUploads({ api, sessionId, onChanged: refresh, onUnauthorized: markUnauthorized });
  const audioUpload = useAudioUpload({ api, sessionId, onChanged: refresh, onUnauthorized: markUnauthorized });
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<UiError | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [opened, setOpened] = useState(false);
  const [actionErrors, setActionErrors] = useState<Record<number, UiError>>({});
  const [finishError, setFinishError] = useState<UiError | null>(null);
  const [confirmFinish, setConfirmFinish] = useState(false);
  const [finishing, setFinishing] = useState(false);

  const parts = useMemo(() => [...(session?.parts ?? [])].sort((a, b) => a.part_no - b.part_no), [session]);
  const display = useMemo(() => sortForDisplay(parts), [parts]);

  // Open the zone that needs attention once, as soon as the server has told us which it is: an interrupted
  // or failed upload first (it needs the user), then the first zone not yet filmed.
  if (!opened && display.length > 0) {
    setOpened(true);
    const attention = display.find((part) => part.state === 'uploading' || part.state === 'failed') ?? display.find((part) => part.state === 'pending');
    setOpen(attention?.part_no ?? display[0]!.part_no);
  }

  const setPartError = useCallback((partNo: number, value: UiError | null) => {
    setActionErrors((current) => {
      const next = { ...current };
      if (value) next[partNo] = value;
      else delete next[partNo];
      return next;
    });
  }, []);

  const handleFailure = useCallback((caught: unknown, partNo: number | null): void => {
    const described = describeError(caught);
    if (described.action === 'new_session') markUnauthorized();
    if (partNo === null) setFinishError(described);
    else setPartError(partNo, described);
    refresh(); // a 409/404 means the server's view moved on; re-read it
  }, [markUnauthorized, refresh, setPartError]);

  const skip = async (part: PartSummary): Promise<void> => {
    setPartError(part.part_no, null);
    try {
      await api.skipPart(part.id);
      refresh();
    } catch (caught) {
      handleFailure(caught, part.part_no);
    }
  };

  const discardStranded = async (part: PartSummary): Promise<void> => {
    if (!part.active_upload_id) return;
    setPartError(part.part_no, null);
    try {
      await api.abortUpload(part.active_upload_id);
      refresh();
    } catch (caught) {
      handleFailure(caught, part.part_no);
    }
  };

  const finalize = async (): Promise<void> => {
    setFinishing(true);
    setFinishError(null);
    setConfirmFinish(false);
    try {
      await api.finalize(sessionId);
      refresh();
    } catch (caught) {
      handleFailure(caught, null);
    } finally {
      setFinishing(false);
    }
  };

  const capturing = session?.state === 'capturing';
  const serverUploading = parts.some((part) => part.state === 'uploading');
  const uploadActive = uploads.anyActive || serverUploading || sourceUpload.blocksFinish;
  const sources = useCaptureSources(api, sessionId, capturing && !unauthorized, undefined, audioUpload.phase === 'accepted');
  const phoneVideoAttached = sourceUpload.source?.state === 'accepted' || parts.some((part) => part.state === 'uploaded' || part.state === 'processing' || part.state === 'processed');
  const kits = kitRows(phoneVideoAttached, sources);
  const unattached = missingKits(kits);
  const canFinish = capturing && !uploadActive && !finishing;
  const notFilmed = parts.filter((part) => part.state === 'pending');
  const skipped = parts.filter((part) => part.state === 'skipped');

  const current = display.find((part) => part.part_no === open) ?? display[0];
  const filmed = parts.filter((part) => part.state === 'uploaded' || part.state === 'processing' || part.state === 'processed').length;
  const currentIndex = current ? display.indexOf(current) : -1;
  const nextPending = display.find((part) => part.state === 'pending' && part.part_no !== current?.part_no);
  // Opening a zone from a tap also speaks its guidance, when the voice guide is on. Audio never gates the zone.
  const openZone = (part: PartSummary): void => {
    setOpen(part.part_no);
    voice.play(zoneLineId(part.zone), (guideFor(part.part_no) ?? GUIDE_STEPS[0]!).goal);
  };
  const nextGuide = nextPending ? (guideFor(nextPending.part_no) ?? GUIDE_STEPS[0]!) : null;

  return (
    <div className="capture">
      {unauthorized && (
        <div role="alert" className="notice notice-error stack">
          <p className="notice-title">Your session has expired or was lost</p>
          <p>
            This browser tab no longer has valid access to its session. Sessions cannot be recovered from another tab or device, and closing the tab can lose them.
            Start a new session to continue; videos from the old session cannot be reattached.
          </p>
          <button type="button" onClick={onNewSession} className="btn btn-sm">Start a new session</button>
        </div>
      )}
      {!unauthorized && error && !session && <ErrorNotice error={error} />}
      {!unauthorized && error && session && (
        <p role="status" className="notice notice-warn">Could not refresh status just now: {error.title}. Showing the last known state.</p>
      )}

      {(session?.state === 'deleting' || session?.state === 'deleted') && <p role="status" className="notice stack">This session has been deleted or is being deleted. <button type="button" className="btn btn-sm" onClick={onNewSession}>Start a new session</button></p>}
      {deleting && <p role="status" className="notice">Deleting session media...</p>}
      {!session && !error && <p role="status" className="muted">Loading your session...</p>}

      {session && !unauthorized && !deleting && session.state !== 'deleting' && session.state !== 'deleted' && (
        <>
          <VoiceGuideBar />
          {singleVideo && <SingleVideoCapture upload={sourceUpload} capturing={capturing && !confirmDelete} />}

          <section aria-labelledby="steps-heading" className="cap">
            <h2 id="steps-heading" className={singleVideo ? 'cap-heading' : 'sr-only'}>{singleVideo ? 'Guided follow-up clips' : 'Capture steps'}</h2>
            <div className="cap-top hide-lg">
              <span>{current ? `Zone ${currentIndex + 1} of ${display.length} · ${(guideFor(current.part_no) ?? GUIDE_STEPS[0]!).title}` : 'Zones'}</span>
              <span className="muted">{filmed} filmed</span>
            </div>
            <nav aria-label="Zones" className="zones">
              <p className="zones-count hide-sm">{filmed} of {display.length} filmed</p>
              <ol className="zones-list">
                {display.map((part, index) => {
                  const guide = guideFor(part.part_no) ?? GUIDE_STEPS[0]!;
                  return (
                    <li key={part.id}>
                      <button type="button" className={`zone zone-${part.state}`} aria-current={part.part_no === current?.part_no ? 'step' : undefined}
                        onClick={() => openZone(part)}>
                        <span className="zn" aria-hidden="true">{index + 1}</span>
                        <span className="zt">{guide.title}{OPTIONAL_PARTS.has(part.part_no) && <span className="opt-tag">Optional</span>}</span>
                        <span className="st" data-testid={`state-${part.part_no}`}>{zoneStateLabel(part.state, uploads.local[part.part_no])}</span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            </nav>
            <div className="cap-main">
              {singleVideo && <p className="hint">After source preparation, add short clips for available missing areas. You can finish without filming every area; missing and unverified areas remain unassessed.</p>}
              {current?.zone === 'dash_electrical' && capturing && (
                <p className="notice" data-testid="engine-check-prompt">
                  This zone is filmed with the engine running. <a href="#engine-check">Do the engine check first</a>, or alongside it.
                </p>
              )}
              {current && (() => {
                const part = current;
                const guide = guideFor(part.part_no) ?? GUIDE_STEPS[0]!;
                return (
                  <PartCard key={part.id} guide={guide} part={part} optional={OPTIONAL_PARTS.has(part.part_no)}
                    local={uploads.local[part.part_no]} record={uploads.records[part.part_no]}
                    hasFile={uploads.getFile(part.part_no) !== undefined} sessionCapturing={capturing && !confirmDelete && (!singleVideo || sourceUpload.followupsAllowed)}
                    actionError={actionErrors[part.part_no]}
                    next={nextPending && nextGuide ? { title: nextGuide.title, go: () => openZone(nextPending) } : null}
                    onStart={(file, contentType) => { setPartError(part.part_no, null); void uploads.start(part.part_no, file, contentType); }}
                    onRetry={() => {
                      const file = uploads.getFile(part.part_no);
                      if (!file) return;
                      const record = uploads.records[part.part_no];
                      if (record) void uploads.resume(part.part_no, file);
                      else void uploads.start(part.part_no, file, normalizeVideoType(file) ?? 'video/mp4');
                    }}
                    onResumeWith={(file) => { void uploads.resume(part.part_no, file); }}
                    onRetake={() => { void uploads.retake(part.part_no); }}
                    onDiscardStranded={() => { void discardStranded(part); }}
                    onSkip={() => { void skip(part); }}
                    onDismissError={() => { setPartError(part.part_no, null); uploads.dismissError(part.part_no); }} />
                );
              })()}

              <div className="note-row">
                <ClockIcon />
                <span><b>Keep this tab open.</b> <span className="muted">Reloading is fine, but closing the tab may lose access to this session and its uploads, with no recovery from another device. An interrupted upload continues only if you select the same video file again; a clip recorded in the page cannot be recovered after a reload.</span></span>
              </div>
              <CaptureKits rows={kits} sessionId={sessionId} ownerToken={ownerToken} capturing={capturing && !confirmDelete} audio={audioUpload} />
              <EngineCheck rows={kits} capturing={capturing && !confirmDelete} />
              <SaveInspection sessionId={sessionId} ownerToken={ownerToken} />

              <Part1Slot part1={parts.find((part) => part.part_no === 1)} verdict={progress?.free_verdict} />

              <section aria-labelledby="finish-heading" className="card finish">
                <h2 id="finish-heading">Finish</h2>
                <ZoneSummary notFilmed={notFilmed} skipped={skipped} />
                {capturing ? (
                  <>
                    {uploadActive && <p className="notice notice-warn" role="status">{singleVideo && sourceUpload.blocksFinish ? 'Source status is loading, an upload is unfinished, or source preparation is pending. Finish becomes available after the source settles; you can discard an unfinished upload.' : 'An upload is still in progress. Finish is available once it completes or you cancel it.'}</p>}
                    {finishError && <ErrorNotice error={finishError} onDismiss={() => setFinishError(null)} />}
                    {confirmFinish ? (
                      <div className="notice stack">
                        <p>Finish capture? This freezes your selection.{notFilmed.length > 0 ? ` ${notFilmed.length} zone${notFilmed.length === 1 ? '' : 's'} not yet filmed will be marked skipped.` : ''}</p>
                        <MissingKitsNotice rows={unattached} />
                        <div className="cap-actions">
                          <button type="button" className="btn btn-dark" disabled={!canFinish} onClick={() => { void finalize(); }}>Yes, finish</button>
                          <button type="button" className="btn btn-ghost" onClick={() => setConfirmFinish(false)}>Keep filming</button>
                        </div>
                      </div>
                    ) : (
                      <div>
                        <button type="button" className="btn btn-dark" disabled={!canFinish} aria-describedby="finish-help" onClick={() => setConfirmFinish(true)}>
                          Finish capture
                        </button>
                      </div>
                    )}
                    <p id="finish-help" className="hint">Capture never finishes on its own. You choose when to finish.</p>
                  </>
                ) : (
                  <p role="status">Capture is finished. The selection is frozen.</p>
                )}
              </section>

              <ProgressPanel api={api} progress={progress} onRefresh={refresh} onUnauthorized={markUnauthorized} />

              <section className="card danger-zone">
                <h2>Delete session media</h2>
                <p className="hint">This removes access immediately and queues deletion of videos and report evidence. Previously downloaded copies cannot be recalled; issued image links can remain valid for up to 60 seconds.</p>
                {deleteError && <ErrorNotice error={deleteError} />}
                {confirmDelete ? <>
                  <p>Delete this session and its media? You cannot undo this.</p>
                  <div className="cap-actions">
                    <button type="button" className="btn btn-rec" onClick={() => {
                      setDeleting(true); setDeleteError(null);
                      void Promise.all([uploads.stopTransfers(), sourceUpload.stopTransfers()]).then(() => api.deleteMedia(sessionId)).then(onNewSession).catch((caught: unknown) => {
                        setDeleteError(describeError(caught)); setDeleting(false); setConfirmDelete(false);
                        if (describeError(caught).action === 'new_session') markUnauthorized();
                        refresh();
                      });
                    }}>Yes, delete session media</button>
                    <button type="button" className="btn btn-ghost" onClick={() => setConfirmDelete(false)}>Keep session</button>
                  </div>
                </> : <div><button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirmDelete(true)}>Delete session media</button></div>}
              </section>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function ClockIcon(): ReactElement {
  return <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><circle cx="10" cy="10" r="7.5" /><path d="M10 6v4l3 2" /></svg>;
}

function ZoneSummary({ notFilmed, skipped }: { notFilmed: PartSummary[]; skipped: PartSummary[] }): ReactElement {
  return (
    <div className="zone-summary">
      <p>
        <span className="lead-in">Skipped zones: </span>
        {skipped.length === 0 ? 'none' : skipped.map((part) => guideFor(part.part_no)?.title ?? `Step ${part.part_no}`).join(', ')}
        {skipped.length > 0 && '. Skipped zones are not assessed and will appear as not assessed in the report.'}
      </p>
      {notFilmed.length > 0 && (
        <p>
          <span className="lead-in">Not filmed yet ({STATE_LABEL.pending.toLowerCase()}): </span>
          {notFilmed.map((part) => guideFor(part.part_no)?.title ?? `Step ${part.part_no}`).join(', ')}. These become skipped if you finish now.
        </p>
      )}
    </div>
  );
}
