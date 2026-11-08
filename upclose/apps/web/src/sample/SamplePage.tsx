import type { ReactElement } from 'react';
import { ReportView } from '../report/ReportView';
import { Button, Pill, Wordmark } from '../ui';
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
            ? <Button size="sm" href={appOrigin}>Start an inspection</Button>
            : <Button variant="secondary" size="sm" href={repoUrl} target="_blank" rel="noopener noreferrer">GitHub</Button>}
        </nav>
      </div></header>
      <main id="main" className="wrap sample-main">
        <p className="muted sample-lede">This is what you get after a walkaround: every finding with its frame, what we couldn't check, and what to ask.</p>
        <ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} header={{
          crumbs: [{ label: 'Home', href: '/' }, { label: 'Sample report' }],
          title: SAMPLE_REPORT.vehicle_label ?? 'Sample vehicle',
          meta: <Pill tone="synthetic">Sample report, synthetic data</Pill>,
        }} />
      </main>
      <footer className="sample-foot"><div className="wrap">
        <span>A visual screening of footage, not a mechanical inspection.</span>
        <a href="/">Back to UpClose</a>
      </div></footer>
    </>
  );
}
