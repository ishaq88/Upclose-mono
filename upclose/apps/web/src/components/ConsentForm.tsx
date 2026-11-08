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
          <p className="muted start-lede">How do you want to film the car? Partial footage is fine; anything not shown stays unassessed.</p>
        </div>
        <fieldset className="tiles">
          <legend className="sr-only">How would you like to capture?</legend>
          <label className="tile">
            <input type="radio" name="capture-mode" value="single_video" checked={captureMode === 'single_video'} onChange={() => setCaptureMode('single_video')} />
            <span className="tile-top"><span className="tag">Quickest</span><span className="radio" aria-hidden="true" /></span>
            <strong>One video</strong>
            <span className="muted">One walk around the car, up to 3 minutes.</span>
          </label>
          <label className="tile">
            <input type="radio" name="capture-mode" value="guided" checked={captureMode === 'guided'} onChange={() => setCaptureMode('guided')} />
            <span className="tile-top"><span className="tag tag-quiet">Most thorough</span><span className="radio" aria-hidden="true" /></span>
            <strong>Guided clips</strong>
            <span className="muted">A short clip per zone, led by a 3D guide.</span>
          </label>
        </fieldset>
        <details className="start-listing">
          <summary>Add seller listing text <span className="optional">(optional)</span></summary>
          <div className="field">
            <label htmlFor="listing">Seller listing text</label>
            <textarea id="listing" value={listing} onChange={(event) => setListing(event.target.value)} maxLength={MAX_LISTING_CHARS} rows={3}
              placeholder="Paste the ad text" aria-describedby="listing-help" />
            <span id="listing-help" className="hint">Plain text, up to {MAX_LISTING_CHARS.toLocaleString()} characters. No personal details.</span>
          </div>
        </details>
        <div className="consent">
          <label className="check" htmlFor="consent">
            <input id="consent" type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} aria-describedby="consent-terms" />
            <span>I understand and agree. I have permission to record or upload this footage, and results are a screening aid, not a mechanic's inspection.</span>
          </label>
          <details className="consent-details">
            <summary>What this means</summary>
            <ul id="consent-terms" className="consent-list">
              <li>Videos you record or select are uploaded for processing. The report labels findings as synthetic test data or real model output.</li>
              <li>Avoid filming people, number plates and private documents.</li>
              <li>You can delete the session's media at any time. Your session lives in this tab, so closing it can lose access to it.</li>
            </ul>
          </details>
        </div>
        {error && <ErrorNotice error={error} />}
        <div className="thumbbar">
          <button type="submit" disabled={!consent || busy} className="btn btn-block" aria-describedby="consent-hint">
            {busy ? 'Starting...' : captureMode === 'single_video' ? 'Continue with one video' : 'Continue with guided clips'}
          </button>
          <p id="consent-hint" className="hint start-hint" aria-live="polite">{consent ? '' : 'Tick the box to continue.'}</p>
        </div>
      </form>
    </section>
  );
}
