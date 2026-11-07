import { useState, type FormEvent, type ReactElement } from 'react';
import { useAccount } from '../accounts/AccountContext';
import type { Api, CaptureMode } from '../api/client';
import { CONSENT_VERSION, MAX_LISTING_CHARS } from '../config';
import { describeError, type UiError } from '../errors';
import { ErrorNotice } from './PartCard';
import '../capture/capture.css';

interface Props {
  api: Api;
  onCreated: (session: { sessionId: string; token: string }) => void;
}

export function ConsentForm({ api, onCreated }: Props): ReactElement {
  const { account } = useAccount();
  const [captureMode, setCaptureMode] = useState<CaptureMode>('single_video');
  const [consent, setConsent] = useState(false);
  const [listing, setListing] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<UiError | null>(null);

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!consent || busy) return;
    setBusy(true);
    setError(null);
    try {
      // The account token only links the inspection to the account. It is never stored as the owner token.
      const created = await api.createSession(CONSENT_VERSION, listing.trim() === '' ? null : listing, captureMode, account?.token ?? null);
      onCreated({ sessionId: created.session_id, token: created.session_token });
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="start-heading">
      <form onSubmit={(event) => { void submit(event); }} className="start" noValidate>
        <div>
          <h1 id="start-heading" className="page-title">New inspection</h1>
          <p className="muted start-lede">How do you want to film the car? Partial footage is welcome; areas not shown or not identifiable remain unassessed.</p>
        </div>
        <fieldset className="tiles">
          <legend className="sr-only">How would you like to capture?</legend>
          <label className="tile">
            <input type="radio" name="capture-mode" value="single_video" checked={captureMode === 'single_video'} onChange={() => setCaptureMode('single_video')} />
            <span className="tile-top"><span className="tag">Quickest</span><span className="radio" aria-hidden="true" /></span>
            <strong>One video</strong>
            <span className="muted">Walk around the car once. Record now or upload a walkaround or partial video, up to 3 minutes.</span>
          </label>
          <label className="tile">
            <input type="radio" name="capture-mode" value="guided" checked={captureMode === 'guided'} onChange={() => setCaptureMode('guided')} />
            <span className="tile-top"><span className="tag tag-quiet">Most thorough</span><span className="radio" aria-hidden="true" /></span>
            <strong>Guided clips</strong>
            <span className="muted">Short clips with a 3D guide for each zone. Skip any zone.</span>
          </label>
        </fieldset>
        <p className="notice notice-warn">Automatic area recognition is not connected — mark sections yourself or use guided clips. Your report labels whether findings are synthetic test data or real model output.</p>
        <div className="field">
          <label htmlFor="listing">Seller listing text <span className="optional">(optional)</span></label>
          <textarea id="listing" value={listing} onChange={(event) => setListing(event.target.value)} maxLength={MAX_LISTING_CHARS} rows={3}
            placeholder="Paste the ad text" aria-describedby="listing-help" />
          <span id="listing-help" className="hint">Plain text only, up to {MAX_LISTING_CHARS.toLocaleString()} characters. Avoid personal details.</span>
        </div>
        <div className="consent">
          <ul id="consent-terms" className="consent-list">
            <li>Videos I record or select are uploaded to this prototype for processing. Findings are labeled in the report as synthetic test data or real model output.</li>
            <li>Results are a screening aid only. They do not replace an inspection by a qualified mechanic.</li>
            <li>I can request deletion from this session. Closing the tab may lose my access to those controls.</li>
          </ul>
          <label className="check" htmlFor="consent">
            <input id="consent" type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} aria-describedby="consent-terms" />
            <span>I understand and agree. I have permission to record or upload this footage and will avoid filming people, number plates and private documents.</span>
          </label>
        </div>
        <p className="notice">
          Your session lives in this browser tab only. Reloading this tab is fine, but closing it may lose access to the session and its uploads. There is no recovery from another device.
        </p>
        {error && <ErrorNotice error={error} />}
        <div className="thumbbar">
          <button type="submit" disabled={!consent || busy} className="btn btn-block" aria-describedby="consent-hint">
            {busy ? 'Starting...' : captureMode === 'single_video' ? 'Continue with one video' : 'Continue with guided clips'}
          </button>
          <p id="consent-hint" className="hint start-hint" aria-live="polite">{consent ? '' : 'Agree above to enable the button.'}</p>
        </div>
      </form>
    </section>
  );
}
