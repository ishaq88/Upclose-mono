import { useState, type FormEvent, type ReactElement } from 'react';
import { useAccount } from '../accounts/AccountContext';
import type { Api, CaptureMode } from '../api/client';
import { CONSENT_VERSION, MAX_LISTING_CHARS } from '../config';
import { describeError, type UiError } from '../errors';
import { SAMPLE_REPORT, sampleFrameUrl } from '../sample/sample';
import { ActionBar, Button, Card, ChoiceTiles, Disclosure, EvidenceFrame, Field, Pill } from '../ui';
import { ErrorNotice } from './PartCard';
import '../ui/intake.css';

interface Props {
  api: Api;
  onCreated: (session: { sessionId: string; token: string }) => void;
}

const MODES = [
  { value: 'single_video', title: 'One video', description: 'One walk around the car, up to 3 minutes.', tag: 'Quickest' },
  { value: 'guided', title: 'Guided clips', description: 'A short clip per zone, led by a 3D guide.', tag: 'Most thorough', tagTone: 'quiet' },
] as const;

/** The synthetic sample finding shown beside the form so people see what they will get. */
const SAMPLE_FINDING = SAMPLE_REPORT.findings.find((finding) => finding.evidence_available) ?? SAMPLE_REPORT.findings[0];

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
    <section aria-labelledby="start-heading" className="intake">
      <form onSubmit={(event) => { void submit(event); }} className="intake-form" noValidate>
        <div>
          <h1 id="start-heading" className="page-title">New inspection</h1>
          <p className="muted intake-lede">About 3 minutes at the car, in daylight. Anything you don't film is marked not checked, never clean.</p>
        </div>
        <ChoiceTiles name="capture-mode" legend="How would you like to capture?" value={captureMode} onChange={setCaptureMode} options={MODES} />
        <Disclosure title="Add seller listing text" summary="Optional">
          <Field label="Seller listing text" hint={`Plain text, up to ${MAX_LISTING_CHARS.toLocaleString()} characters. No personal details.`}>
            <textarea value={listing} onChange={(event) => setListing(event.target.value)} maxLength={MAX_LISTING_CHARS} rows={3} placeholder="Paste the ad text" />
          </Field>
        </Disclosure>
        <div className="intake-consent">
          <label className="check" htmlFor="consent">
            <input id="consent" type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} aria-describedby="consent-terms" />
            <span>I understand and agree. I have permission to record or upload this footage, and results are a screening aid, not a mechanic's inspection.</span>
          </label>
          <Disclosure title="What this means" className="intake-terms">
            <ul id="consent-terms" className="consent-list">
              <li>Videos you record or select are uploaded for processing. The report labels findings as synthetic test data or real model output.</li>
              <li>Avoid filming people, number plates and private documents.</li>
              <li>You can delete the session's media at any time. Your session lives in this tab, so closing it can lose access to it.</li>
            </ul>
          </Disclosure>
        </div>
        {error && <ErrorNotice error={error} />}
        <ActionBar className="intake-actions">
          <Button type="submit" block disabled={!consent || busy} aria-describedby="consent-hint">
            {busy ? 'Starting...' : captureMode === 'single_video' ? 'Continue with one video' : 'Continue with guided clips'}
          </Button>
          <p id="consent-hint" className="hint start-hint" aria-live="polite">{consent ? '' : 'Tick the box to continue.'}</p>
          <p className="hint intake-price">Driver side free · full report $19</p>
        </ActionBar>
      </form>
      {SAMPLE_FINDING && (
        <Card as="aside" padding="lg" className="intake-sample" aria-label="A sample finding">
          <Pill tone="synthetic">Synthetic sample</Pill>
          <EvidenceFrame src={sampleFrameUrl(SAMPLE_FINDING.id)} bbox={SAMPLE_FINDING.bbox} timestampMs={SAMPLE_FINDING.source_timestamp_ms}
            label="Sample finding: rust on the driver-side sill" />
          <ul className="intake-ticks">
            <li>Every finding comes with the exact frame it was found in.</li>
            <li>Zones you don't film are listed as not checked.</li>
            <li>You leave with questions for the seller and a mechanic.</li>
          </ul>
        </Card>
      )}
    </section>
  );
}
