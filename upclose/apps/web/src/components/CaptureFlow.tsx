import { useCallback, useMemo, useRef, useState, type ReactElement } from 'react';
import { useAccount } from '../accounts/AccountContext';
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
import { CaptureKits } from './CaptureKits';
import { GuideViewer } from '../capture/GuideViewer';
import { kitRows, missingKits } from '../capture/kits';
import { useCaptureSources } from '../capture/useCaptureSources';
import { useAudioUpload } from '../upload/audioUpload';
import { useVoiceGuide, VoiceGuideBar, VoiceGuideProvider, zoneLineId } from '../capture/voiceGuide';
import { EngineCheck } from './EngineCheck';
import { UnlockTeaser, WrapUp } from './WrapUp';
import { useCaptureHeader } from '../capture/headerSlot';
import { ActionBar } from '../ui/ActionBar';
import { Banner } from '../ui/Banner';
import { Button } from '../ui/Button';
import { Card } from '../ui/Card';
import { Notice } from '../ui/Notice';
import { Pill } from '../ui/Pill';
import { cx } from '../ui/cx';
import '../capture/capture.css';

interface Props {
  api: Api;
  sessionId: string;
  /** The inspection's owner token, passed only to SaveInspection so it can link the inspection to an account. */
  ownerToken: string;
  /** Forget the stored session so the consent screen shows again. */
  onNewSession: () => void;
  /** Which view to open on (the dev gallery uses this; real sessions always start on a zone). */
  initialView?: 'engine' | 'wrapup';
}

export function CaptureFlow(props: Props): ReactElement {
  return <VoiceGuideProvider api={props.api}><CaptureFlowBody {...props} /></VoiceGuideProvider>;
}

function CaptureFlowBody({ api, sessionId, ownerToken, onNewSession, initialView }: Props): ReactElement {
  const voice = useVoiceGuide();
  const { session, progress, error, unauthorized, refresh, recheckAccess } = useSession(api, sessionId);
  const singleVideo = session?.capture_mode === 'single_video';
  const sourceUpload = useSourceUpload({ api, sessionId, enabled: singleVideo && session?.state !== 'deleting' && session?.state !== 'deleted' && !unauthorized, onChanged: refresh, onUnauthorized: recheckAccess });
  const uploads = useUploads({ api, sessionId, onChanged: refresh, onUnauthorized: recheckAccess });
  const audioUpload = useAudioUpload({ api, sessionId, onChanged: refresh, onUnauthorized: recheckAccess });
  const { account } = useAccount();
  // The report address survives losing the session read, so a signed-in owner can still reach it.
  const reportSlug = useRef<string | null>(null);
  if (progress?.report?.slug) reportSlug.current = progress.report.slug;
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<UiError | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [opened, setOpened] = useState(false);
  const [actionErrors, setActionErrors] = useState<Record<number, UiError>>({});
  const [finishError, setFinishError] = useState<UiError | null>(null);
  const [wrapup, setWrapup] = useState(initialView === 'wrapup');
  const [engineOpen, setEngineOpen] = useState(initialView === 'engine');
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
    if (described.action === 'new_session') recheckAccess();
    if (partNo === null) setFinishError(described);
    else setPartError(partNo, described);
    refresh(); // a 409/404 means the server's view moved on; re-read it
  }, [recheckAccess, refresh, setPartError]);

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
    try {
      await api.finalize(sessionId);
      setWrapup(false);
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
    setEngineOpen(false);
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

  const checking = parts.filter((part) => part.state === 'processing').length;
  useCaptureHeader(filmed, display.length, checking, capturing && !unauthorized ? () => setWrapup(true) : null);
  const slotRef = useRef<HTMLDivElement | null>(null);
  /** The phone banner's action: open the free-result card and bring it into view. */
  const showFreeResult = (): void => {
    const details = slotRef.current?.querySelector('details');
    if (details) details.open = true;
    slotRef.current?.scrollIntoView({ block: 'start' });
  };
  const openEngine = (): void => { setEngineOpen(true); setWrapup(false); };
  const attachedKits = kits.filter((row) => row.id !== 'phone_video' && row.attached).length;
  const kitTotal = kits.filter((row) => row.id !== 'phone_video').length;
  const blockedLabel = uploadActive ? 'Waiting for the upload to finish' : waitingVideos > 0 ? `Waiting for ${waitingVideos} clip${waitingVideos === 1 ? '' : 's'} to start` : null;
  const showWrapUp = wrapup && capturing;
  const freeReady = Boolean(progress?.free_verdict);

  return (
    <div className="capture">
      {unauthorized && (
        <Notice tone="error" title="Your session has expired or was lost"
          action={<div className="cap-actions">
            {account && reportSlug.current && <Button size="sm" variant="secondary" href={`/report/${encodeURIComponent(reportSlug.current)}`}>Open this inspection's report</Button>}
            {account && <Button size="sm" variant="secondary" href="/reports">Go to My reports</Button>}
            <Button size="sm" onClick={onNewSession}>Start a new session</Button>
          </div>}>
          <p>This tab no longer has access to its session, and it cannot be recovered from another tab or device. Start a new one to continue.</p>
          {account && <p>You are signed in, so an inspection saved to your account is still available there.</p>}
        </Notice>
      )}
      {!unauthorized && error && !session && <ErrorNotice error={error} />}
      {!unauthorized && error && session && (
        <Notice tone="warn"><p role="status">Could not refresh status just now: {error.title}. Showing the last known state.</p></Notice>
      )}

      {(session?.state === 'deleting' || session?.state === 'deleted') && (
        <Notice action={<Button size="sm" onClick={onNewSession}>Start a new session</Button>}>
          <p role="status">This session has been deleted or is being deleted.</p>
        </Notice>
      )}
      {deleting && <Notice><p role="status">Deleting session media...</p></Notice>}
      {!session && !error && <p role="status" className="muted">Loading your session...</p>}

      {session && !unauthorized && !deleting && session.state !== 'deleting' && session.state !== 'deleted' && (showWrapUp ? (
        <WrapUp
          parts={display} done={doneParts} unattached={unattached}
          notFilmedCount={notFilmed.length}
          blockedLabel={blockedLabel} blockedDetail={finishState} blockedTestId={!uploadActive && waitingVideos > 0 ? 'finish-waiting' : undefined}
          finishing={finishing} finishError={finishError} onDismissError={() => setFinishError(null)}
          onFinish={() => { void finalize(); }} onKeepFilming={() => setWrapup(false)} onOpenEngine={openEngine}
          side={<>
            <ProgressPanel compact api={api} progress={progress} onRefresh={refresh} onUnauthorized={recheckAccess} />
            <UnlockTeaser />
          </>}
        />
      ) : (
        <>
          {singleVideo && <SingleVideoCapture upload={sourceUpload} capturing={capturing && !confirmDelete} />}

          <section aria-labelledby="steps-heading" className="cap2">
            <h2 id="steps-heading" className={singleVideo ? 'cap-heading' : 'sr-only'}>{singleVideo ? 'Guided follow-up clips' : 'Capture steps'}</h2>
            <div className="cap-body">
              <nav aria-label="Zones" className="zones">
                <p className="zones-count">{filmed} of {display.length} filmed</p>
                <ol className="zones-list">
                  {display.map((part, index) => {
                    const guide = guideFor(part.part_no) ?? GUIDE_STEPS[0]!;
                    const here = !engineOpen && part.part_no === current?.part_no;
                    return (
                      <li key={part.id}>
                        <button type="button" className={`zone zone-${part.state}`} aria-current={here ? 'step' : undefined} onClick={() => openZone(part)}>
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
                  <li>
                    <button type="button" className={cx('zone', 'zone-engine', attachedKits > 0 && 'zone-uploaded')} aria-current={engineOpen ? 'step' : undefined} onClick={() => setEngineOpen(true)}>
                      <span className="zn" aria-hidden="true">{attachedKits > 0 ? <CheckIcon /> : <EngineIcon />}</span>
                      <span className="zt"><span className="zt-name">Engine check<span className="opt-tag">Optional</span></span></span>
                      <span className="st" data-testid="state-engine">{attachedKits} of {kitTotal} attached</span>
                    </button>
                  </li>
                </ol>
              </nav>

              {capturing && freeReady && (
                <div className="cap-banner">
                  <Banner tone="ok" action={<Button variant="text" size="sm" onClick={showFreeResult}>View</Button>}>Driver side result is ready</Banner>
                </div>
              )}

              <div className="cap-main">
                <div className="cap-pane" hidden={engineOpen}>
                  {currentGuide && <div className="cap-car"><GuideViewer guide={currentGuide} done={doneParts} position={`${currentIndex + 1}/${display.length}`} /></div>}
                  <div className="cap-top">
                    <span className="cap-pos">{current ? `Zone ${currentIndex + 1} of ${display.length}` : 'Zones'}</span>
                    <VoiceGuideBar />
                  </div>
                  {singleVideo && <p className="hint">Add short clips for areas the video missed. You can finish without filming every area; the rest stay unassessed.</p>}
                  {current?.zone === 'dash_electrical' && capturing && (
                    <div data-testid="engine-check-prompt">
                      <Notice tone="info">
                        <p>Filmed with the engine running. <a href="#engine-check" onClick={(event) => { event.preventDefault(); setEngineOpen(true); }}>Do the engine check first</a>, or alongside it.</p>
                      </Notice>
                    </div>
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
                </div>

                {/* Mounted while hidden so a half-finished engine check and its attach state survive a look at another zone. */}
                <section className="cap-pane engine-panel" hidden={!engineOpen} aria-labelledby="engine-step-heading">
                  <div className="engine-head">
                    <h2 id="engine-step-heading" className="step-title">Engine check</h2>
                    <Pill tone="optional">Optional</Pill>
                  </div>
                  <Card className="stack">
                    <CaptureKits rows={kits} sessionId={sessionId} ownerToken={ownerToken} capturing={capturing && !confirmDelete} audio={audioUpload} />
                  </Card>
                  <Card className="stack">
                    <EngineCheck rows={kits} capturing={capturing && !confirmDelete} />
                  </Card>
                </section>
              </div>
            </div>

            <aside className="cap-session" aria-label="Inspection status">
              <div className={cx('cap-progress', capturing && 'cap-progress-capturing')}>
                <ProgressPanel compact api={api} progress={progress} onRefresh={refresh} onUnauthorized={recheckAccess} />
                <p className="rail-note">You can keep going; we process in the background. Keep this tab open while uploading.</p>
              </div>
              <div ref={slotRef} className="cap-slot"><Part1Slot part1={parts.find((part) => part.part_no === 1)} verdict={progress?.free_verdict} /></div>
              <Card as="section" className="cap-engine-card stack" aria-labelledby="engine-entry-heading">
                <h2 id="engine-entry-heading" className="sub-title">Engine check <span className="opt-tag">Optional</span></h2>
                <p className="hint">Pod, OBD and engine audio feed the Mechanical score. {attachedKits} of {kitTotal} attached.</p>
                <div><Button variant="secondary" size="sm" onClick={() => setEngineOpen(true)}>Open engine check</Button></div>
              </Card>

              <Card as="section" aria-labelledby="finish-heading" className="finish stack">
                <h2 id="finish-heading" className="sub-title">Finish</h2>
                <ZoneSummary notFilmed={notFilmed} skipped={skipped} capturing={capturing} />
                {capturing ? (
                  <>
                    {finishState && <p className="finish-state" role="status" data-testid={!uploadActive && waitingVideos > 0 ? 'finish-waiting' : undefined}>{finishState}</p>}
                    {finishError && <ErrorNotice error={finishError} onDismiss={() => setFinishError(null)} />}
                    <div><Button variant="dark" disabled={!canFinish} onClick={() => setWrapup(true)}>Finish capture</Button></div>
                  </>
                ) : (
                  <p role="status">Capture is finished. The selection is frozen.</p>
                )}
              </Card>

              <div className="cap-foot">
                <SaveInspection sessionId={sessionId} ownerToken={ownerToken} />
                <section className="danger-zone" aria-label="Delete session media">
                  {deleteError && <ErrorNotice error={deleteError} />}
                  {confirmDelete ? (
                    <Card tone="danger-outline" padding="md" className="stack">
                      <p>Delete this session and its media? You cannot undo this. Issued image links can stay valid for up to 60 seconds.</p>
                      <div className="cap-actions">
                        <Button variant="danger" onClick={() => {
                          setDeleting(true); setDeleteError(null);
                          void Promise.all([uploads.stopTransfers(), sourceUpload.stopTransfers()]).then(() => api.deleteMedia(sessionId)).then(onNewSession).catch((caught: unknown) => {
                            setDeleteError(describeError(caught)); setDeleting(false); setConfirmDelete(false);
                            if (describeError(caught).action === 'new_session') recheckAccess();
                            refresh();
                          });
                        }}>Yes, delete session media</Button>
                        <Button variant="secondary" onClick={() => setConfirmDelete(false)}>Keep session</Button>
                      </div>
                    </Card>
                  ) : <Button variant="text" size="sm" onClick={() => setConfirmDelete(true)}>Delete session media</Button>}
                </section>
              </div>
            </aside>
          </section>
        </>
      ))}
    </div>
  );
}

function EngineIcon(): ReactElement {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M2 6h2l1-2h5l1 2h2v5H2zM5 11v2M11 11v2" /></svg>;
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

function ZoneSummary({ notFilmed, skipped, capturing }: { notFilmed: PartSummary[]; skipped: PartSummary[]; capturing: boolean }): ReactElement {
  const title = (part: PartSummary): string => guideFor(part.part_no)?.title ?? `Step ${part.part_no}`;
  return (
    <div className="zone-summary">
      {skipped.length > 0 && (
        <p>
          <span className="lead-in">Skipped zones: </span>
          {skipped.map(title).join(', ')}. They will show as not assessed.
        </p>
      )}
      {capturing && notFilmed.length > 0 && <p>{notFilmed.length} zone{notFilmed.length === 1 ? '' : 's'} not filmed yet will be marked skipped.</p>}
    </div>
  );
}
