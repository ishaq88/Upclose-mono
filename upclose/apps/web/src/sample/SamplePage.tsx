import type { ReactElement } from 'react';
import { ReportView } from '../report/ReportView';
import { Wordmark } from '../ui/Wordmark';
import { SAMPLE_REPORT, sampleFrameUrl } from './sample';

export function SamplePage({ appOrigin, repoUrl }: { appOrigin: string | null; repoUrl: string }): ReactElement {
  return (
    <>
      <a className="skip-link" href="#main">Skip to report</a>
      <header className="app-head"><div className="wrap">
        <Wordmark />
        <nav className="app-nav" aria-label="Main">
          <a href="/">Home</a>
          {appOrigin
            ? <a className="btn btn-sm" href={appOrigin}>Start an inspection</a>
            : <a className="btn btn-sm btn-ghost" href={repoUrl} target="_blank" rel="noopener noreferrer">GitHub</a>}
        </nav>
      </div></header>
      <main id="main" className="wrap sample-main">
        <div className="sample-head">
          <p className="sample-tag">Sample report</p>
          <h1>{SAMPLE_REPORT.vehicle_label ?? 'Sample vehicle'}</h1>
          <p className="muted">This is what you get after a walkaround: every finding with its frame, what we couldn't check, and what to ask.</p>
        </div>
        <ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />
      </main>
      <footer className="sample-foot"><div className="wrap">
        <span>A visual screening of footage, not a mechanical inspection.</span>
        <a href="/">Back to UpClose</a>
      </div></footer>
    </>
  );
}
