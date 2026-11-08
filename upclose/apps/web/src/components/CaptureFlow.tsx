import { useCallback, useMemo, useState, type ReactElement } from 'react';
import { ApiError, type Api, type PartSummary } from '../api/client';
import { SaveInspection } from '../accounts/SaveInspection';
import { GUIDE_STEPS, guideFor } from '../capture/guide';
import { OPTIONAL_PARTS, sortForDisplay } from '../capture/zones';
import { normalizeVideoType } from '../capture/media';
import { describeError, type UiError } from '../errors';
import { useSession } from '../session/useSession';
import { useSourceUpload } from '../upload/useSourceUpload';
import { SingleVideoCapture } from './SingleVideoCapture';
import { useUploads } from '../upload/useUploads';
import { ErrorNotice, PartCard, zoneStateLabel } from './PartCard';
import { Part1Slot } from './FreeVerdictCard';
import { ProgressPanel } from './ProgressPanel';
import { CaptureKits, MissingKitsNotice } from './CaptureKits';
import { GuideViewer } from '../capture/GuideViewer';
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
      // A generic 409 from finalize means work is still queued, not that the page was stale. Other 409 codes keep their own text.
      if (caught instanceof ApiError && caught.status === 409 && caught.code !== 'UPLOAD_INCOMPLETE' && caught.code !== 'REPORT_NOT_READY') {
        setFinishError({ title: 'Not quite ready to finish', message: 'A video is still waiting to be processed. Try Finish again in a minute.', action: 'retry' });
      }
    } finally {
      setFinishing(false);
    }
  };

  const capturing = session?.state === 'capturing';
  const serverUploading = parts.some((part) => part.state === 'uploading');
  const uploadActive = uploads.anyActive || serverUploading || audioUpload.busy || sourceUpload.blocksFinish;
  // The page sees a clip as 'uploaded' until processing starts, then 'processing'; 'uploaded' is the queued, not-yet-started state.
  const waitingVideos = parts.filter((part) => part.state === 'uploaded').length;
  const sources = useCaptureSources(api, sessionId, capturing && !unauthorized, undefined, audioUpload.phase === 'attached');
  const phoneVideoAttached = sourceUpload.source?.state === 'accepted' || parts.some((part) => part.state === 'uploaded' || part.state === 'processing' || part.state === 'processed');
  const kits = kitRows(phoneVideoAttached, sources);
  const unattached = missingKits(kits);
  const canFinish = capturing && !uploadActive && waitingVideos === 0 && !finishing;
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

  const doneParts = parts.filter((part) => part.state === 'uploaded' || part.state === 'processing' || part.state === 'processed').map((part) => part.part_no);
  const currentGuide = current ? (guideFor(current.part_no) ?? GUIDE_STEPS[0]!) : null;
  const finishState = ((): string | null => {
    if (!capturing) return null;
    if (uploadActive) return singleVideo && sourceUpload.blocksFinish && !uploads.anyActive && !audioUpload.busy ? 'Finish unlocks once the source video settles. You can discard an unfinished upload.' : 'An upload is still in progress. Finish unlocks when it completes or you cancel it.';
    if (waitingVideos > 0) return `Waiting for ${waitingVideos} video${waitingVideos === 1 ? '' : 's'} to start processing. Finish unlocks as soon as ${waitingVideos === 1 ? 'it starts' : 'they start'}; the report keeps building in the background.`;
    return null;
  })();

  return (
    <div className="capture">
      {unauthorized && (
        <div role="alert" className="notice notice-error stack">
          <p className="notice-title">Your session has expired or was lost</p>
          <p>This tab no longer has access to its session, and it cannot be recovered from another tab or device. Start a new one to continue.</p>
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
          {singleVideo && <SingleVideoCapture upload={sourceUpload} capturing={capturing && !confirmDelete} />}

          <section aria-labelledby="steps-heading" className="cap2">
            <h2 id="steps-heading" className={singleVideo ? 'cap-heading' : 'sr-only'}>{singleVideo ? 'Guided follow-up clips' : 'Capture steps'}</h2>
            <div className="cap-left">
              <div className="cap-side">
                {currentGuide && <div className="cap-car"><GuideViewer guide={currentGuide} done={doneParts} position={`${currentIndex + 1}/${display.length}`} /></div>}
                <nav aria-label="Zones" className="zones">
                  <p className="zones-count">{filmed} of {display.length} filmed</p>
                  <ol className="zones-list">
                    {display.map((part, index) => {
                      const guide = guideFor(part.part_no) ?? GUIDE_STEPS[0]!;
                      return (
                        <li key={part.id}>
                          <button type="button" className={`zone zone-${part.state}`} aria-current={part.part_no === current?.part_no ? 'step' : undefined}
                            onClick={() => openZone(part)}>
                            <span className="zn" aria-hidden="true">{part.state === 'processed' || part.state === 'uploaded' || part.state === 'processing' ? <CheckIcon /> : index + 1}</span>
                            <span className="zt">
                              <span className="zt-name">{guide.title}{OPTIONAL_PARTS.has(part.part_no) && <span className="opt-tag">Optional</span>}</span>
                              <MiniRail state={part.state} />
                            </span>
                            <span className="st" data-testid={`state-${part.part_no}`}>{zoneStateLabel(part.state, uploads.local[part.part_no])}</span>
                          </button>
                        </li>
                      );
                    })}
                  </ol>
                </nav>
              </div>
              <div className="cap-rail">
                <ProgressPanel compact api={api} progress={progress} onRefresh={refresh} onUnauthorized={markUnauthorized} />
                <p className="rail-note">You can keep going; we process in the background. Keep this tab open while uploading.</p>
              </div>
            </div>

            <div className="cap-main">
              <div className="cap-top">
                <span className="cap-pos">{current ? `Zone ${currentIndex + 1} of ${display.length} · ${(guideFor(current.part_no) ?? GUIDE_STEPS[0]!).title}` : 'Zones'}</span>
                <VoiceGuideBar />
              </div>
              {singleVideo && <p className="hint">Add short clips for areas the video missed. You can finish without filming every area; the rest stay unassessed.</p>}
              {current?.zone === 'dash_electrical' && capturing && (
                <p className="notice" data-testid="engine-check-prompt">
                  Filmed with the engine running. <a href="#engine-check">Do the engine check first</a>, or alongside it.
                </p>
              )}
              {current && (() => {
                const part = current;
                const guide = guideFor(part.part_no) ?? GUIDE_STEPS[0]!;
                return (
                  <PartCard key={part.id} guide={guide} part={part} optional={OPTIONAL_PARTS.has(part.part_no)} showViewer={false}
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

              <section aria-labelledby="engine-step-heading" className="card engine-panel">
                <h2 id="engine-step-heading" className="step-title">Engine check</h2>
                <CaptureKits rows={kits} sessionId={sessionId} ownerToken={ownerToken} capturing={capturing && !confirmDelete} audio={audioUpload} />
                <EngineCheck rows={kits} capturing={capturing && !confirmDelete} />
              </section>

              <Part1Slot part1={parts.find((part) => part.part_no === 1)} verdict={progress?.free_verdict} />

              <section aria-labelledby="finish-heading" className="card finish">
                <h2 id="finish-heading" className="step-title">Finish</h2>
                <ZoneSummary notFilmed={notFilmed} skipped={skipped} />
                {capturing ? (
                  <>
                    {finishState && <p className="finish-state" role="status" data-testid={!uploadActive && waitingVideos > 0 ? 'finish-waiting' : undefined}>{finishState}</p>}
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
                        <button type="button" className="btn btn-dark btn-finish" disabled={!canFinish} onClick={() => setConfirmFinish(true)}>
                          Finish capture
                        </button>
                      </div>
                    )}
                  </>
                ) : (
                  <p role="status">Capture is finished. The selection is frozen.</p>
                )}
              </section>

              <div className="cap-foot">
                <SaveInspection sessionId={sessionId} ownerToken={ownerToken} />
                <section className="danger-zone" aria-label="Delete session media">
                  {deleteError && <ErrorNotice error={deleteError} />}
                  {confirmDelete ? <>
                    <p>Delete this session and its media? You cannot undo this. Issued image links can stay valid for up to 60 seconds.</p>
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
                  </> : <button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirmDelete(true)}>Delete session media</button>}
                </section>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function CheckIcon(): ReactElement {
  return <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M2.5 7.5l3 3 6-7" /></svg>;
}

/** Four-step micro rail for one zone: Uploaded, Finding car, Checking damage, Done. Decorative; the state text beside it is the label. */
function MiniRail({ state }: { state: PartSummary['state'] }): ReactElement | null {
  const lit = state === 'uploaded' ? 1 : state === 'processing' ? 2 : state === 'processed' ? 4 : 0;
  if (state === 'pending' || state === 'skipped') return null;
  return (
    <span className={`mini mini-${state}`} aria-hidden="true" title="Uploaded, finding car, checking damage, done">
      {[0, 1, 2, 3].map((i) => <i key={i} className={i < lit ? 'on' : i === lit && (state === 'processing' || state === 'uploading') ? 'run' : undefined} />)}
    </span>
  );
}

function ZoneSummary({ notFilmed, skipped }: { notFilmed: PartSummary[]; skipped: PartSummary[] }): ReactElement {
  const title = (part: PartSummary): string => guideFor(part.part_no)?.title ?? `Step ${part.part_no}`;
  return (
    <div className="zone-summary">
      <p>
        <span className="lead-in">Skipped zones: </span>
        {skipped.length === 0 ? 'none' : skipped.map(title).join(', ')}
        {skipped.length > 0 && '. They will show as not assessed.'}
      </p>
      {notFilmed.length > 0 && (
        <p>
          <span className="lead-in">Not filmed yet: </span>
          {notFilmed.map(title).join(', ')}. These become skipped if you finish now.
        </p>
      )}
    </div>
  );
}
