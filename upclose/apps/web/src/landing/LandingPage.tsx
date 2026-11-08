import type { ReactElement } from 'react';
import { findingTitle, formatIndex, formatTime, notChecked, OPTIONAL_ZONES, orderFindings, scoredZones, verdict, ZONE_LABEL } from '../report/model';
import { HERO_PHOTO, HERO_POINTS, SAMPLE_REPORT, sampleFrameUrl } from '../sample/sample';
import { Button, Pill, Wordmark } from '../ui';
import { HeroLoupe, type LoupePoint } from './HeroLoupe';

export interface LandingPageProps {
  /** The live app's origin. Null while the app is not public yet: product actions then point to the sample and the code. */
  appOrigin: string | null;
  repoUrl: string;
}

const SAMPLE_HREF = '/sample/';

function Check(): ReactElement {
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <circle cx="11" cy="11" r="10" fill="#ff5a1f" /><path d="M6.5 11.5l3 3 6-6.5" fill="none" stroke="#111315" strokeWidth="2.2" />
    </svg>
  );
}

function GitHubMark(): ReactElement {
  return (
    <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">
      <path d="M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38v-1.33c-2.23.48-2.7-1.07-2.7-1.07-.36-.92-.89-1.17-.89-1.17-.73-.5.06-.49.06-.49.8.06 1.23.83 1.23.83.71 1.22 1.87.87 2.33.66.07-.52.28-.87.5-1.07-1.78-.2-3.65-.89-3.65-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.28.82 2.15 0 3.07-1.87 3.75-3.66 3.95.29.25.54.73.54 1.48v2.2c0 .21.15.46.55.38A8 8 0 0 0 8 0Z" />
    </svg>
  );
}

export function LandingPage({ appOrigin, repoUrl }: LandingPageProps): ReactElement {
  const live = appOrigin !== null;
  const findings = orderFindings(SAMPLE_REPORT.findings);
  const v = verdict(findings);
  const zones = SAMPLE_REPORT.scores.zones;
  const scorable = zones.filter((z) => !OPTIONAL_ZONES.has(z.zone)).length;
  const missing = notChecked(zones).filter((z) => !OPTIONAL_ZONES.has(z.zone));
  const top = findings[0];
  const driver = zones.find((z) => z.zone === 'driver_side');
  const points: LoupePoint[] = findings.flatMap((f) => {
    const at = HERO_POINTS[f.id];
    const [what, ...where] = findingTitle(f).split(', ');
    return at ? [{ ...at, title: what ?? '', detail: where.join(', ').replace(/^./, (c) => c.toUpperCase()), time: formatTime(f.source_timestamp_ms) }] : [];
  });

  const primary = live
    ? <Button href={appOrigin}>Start an inspection</Button>
    : <Button href={SAMPLE_HREF}>See a sample report</Button>;
  const secondary = live
    ? <Button variant="text" href={SAMPLE_HREF}>See a sample report</Button>
    : <Button variant="text" href={repoUrl} target="_blank" rel="noopener noreferrer" icon={<GitHubMark />}>View the code on GitHub</Button>;

  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="site-head"><div className="wrap">
        <Wordmark />
        <nav className="site-nav" aria-label="Main">
          <a className="hide-sm" href="#how">How it works</a>
          <a className="hide-sm" href={SAMPLE_HREF}>Sample report</a>
          <a className="hide-sm" href="#pricing">Pricing</a>
          {live
            ? <Button size="sm" href={appOrigin}>Start an inspection</Button>
            : <Button variant="secondary" size="sm" href={repoUrl} target="_blank" rel="noopener noreferrer" icon={<GitHubMark />}><span className="hide-xs">GitHub</span></Button>}
        </nav>
      </div></header>

      <main id="main">
        <section className="wrap hero" aria-labelledby="hero-h">
          <div className="hero-top">
            <h1 id="hero-h">Good find.<br />Look closer.</h1>
            <div className="hero-copy">
              <p className="lede">Film a used car on your phone before you buy it. UpClose checks the footage for rust, dents, scratches, cracked glass, worn tires and leaks, and shows you the exact frame where it found each one.</p>
              <div className="hero-cta">{primary}{secondary}</div>
              <p className="price-note">{live ? 'Driver side free. Full report $19.' : 'The app opens to the public soon. Driver side will be free; the full report $19.'}</p>
            </div>
          </div>
          <HeroLoupe src={HERO_PHOTO.src} width={HERO_PHOTO.width} height={HERO_PHOTO.height} points={points}
            alt={`A white panel van with ${points.length} findings marked: ${points.map((p) => `${p.title.toLowerCase()} on the ${p.detail.toLowerCase()}`).join(', ')}.`} />
        </section>

        <section id="how" className="wrap section" aria-labelledby="how-h">
          <h2 id="how-h" className="h-xl">About three minutes<br />at the car.</h2>
          <ol className="steps">
            <li className="step"><span className="step-k">Step 1</span><h3>Film the car</h3>
              <p>Walk around it once, or follow eight short guided clips. Skip anything you can't reach.</p>
              <div className="step-vis chips" aria-hidden="true"><span className="chip chip-on">Driver side</span><span className="chip">Passenger side</span><span className="chip">Front &amp; rear</span><span className="chip">Tires</span><span className="chip chip-hatch">Underbody</span></div></li>
            <li className="step"><span className="step-k">Step 2</span><h3>We check every frame</h3>
              <p>UpClose finds the vehicle in each frame, ignores everything around it, and looks for damage on the vehicle itself.</p>
              {top && <div className="step-vis step-frame" aria-hidden="true"><img src={sampleFrameUrl(top.id)} alt="" loading="lazy" /><span className="ev-stamp">{formatTime(top.source_timestamp_ms)}</span></div>}</li>
            <li className="step"><span className="step-k">Step 3</span><h3>Know what to ask</h3>
              <p>Every finding comes with its frame and timestamp, plus what couldn't be checked and questions for the seller.</p>
              <div className="step-vis step-pills" aria-hidden="true">
                {driver?.score != null && <Pill tone="ok">Driver side · {formatIndex(driver.score)}</Pill>}
                {missing[0] && <Pill tone="not">{ZONE_LABEL[missing[missing.length - 1]!.zone]} · not checked</Pill>}
              </div></li>
          </ol>
        </section>

        <section id="inside" className="dark" aria-labelledby="inside-h">
          <div className="wrap dark-grid">
            <div className="dark-copy">
              <h2 id="inside-h" className="h-xl">A report you can take to the seller.</h2>
              <ul>
                <li><Check /><span>Each finding with the frame and second it was found</span></li>
                <li><Check /><span>Zones that couldn't be checked, listed plainly</span></li>
                <li><Check /><span>The seller's listing checked against your footage</span></li>
                <li><Check /><span>Repair cost ranges and a checklist for your mechanic</span></li>
              </ul>
              <Button href={SAMPLE_HREF}>Open the sample report</Button>
            </div>
            <a className="phone" href={SAMPLE_HREF} aria-label="Open the sample report">
              <div className="phone-in">
                <div className="phone-top"><span className="wordmark wordmark-xs">upclose<i /></span><span>{SAMPLE_REPORT.vehicle_label}</span></div>
                <div><b className="phone-verdict">{v.headline}</b><span className="phone-sub">{SAMPLE_REPORT.scores.headline !== null ? `Index ${formatIndex(SAMPLE_REPORT.scores.headline)} · ` : ''}based on {scoredZones(zones).length} of {scorable} zones</span></div>
                {top && <div className="phone-print"><div className="ev-frame"><img src={sampleFrameUrl(top.id)} alt="" loading="lazy" /><span className="ev-stamp">{formatTime(top.source_timestamp_ms)}</span></div><b>1 · {findingTitle(top)}</b></div>}
                <div className="phone-pills">{missing.map((z) => <Pill key={z.zone} tone="not">{ZONE_LABEL[z.zone]} · {z.state === 'partial' ? 'partly checked' : 'not checked'}</Pill>)}</div>
              </div>
            </a>
          </div>
        </section>

        <section id="pricing" className="wrap section" aria-labelledby="price-h">
          <h2 id="price-h" className="h-xl">Start free.<br />Pay if it's worth it.</h2>
          <div className="plans">
            <div className="plan"><h3>Driver side</h3><div className="price">Free</div>
              <ul><li>Condition index for the driver side</li><li>Driver-side findings with frames</li><li>See which zones you've covered</li></ul>
              {live && <Button variant="secondary" href={appOrigin}>Start free</Button>}</div>
            <div className="plan plan-hot"><h3>Full report</h3><div className="price">$19 <span>per vehicle</span></div>
              <ul><li>All eight zones</li><li>Every finding with its frame</li><li>Listing claims checked</li><li>Repair cost ranges</li><li>A checklist for the seller and your mechanic</li></ul>
              <Button href={live ? appOrigin : SAMPLE_HREF}>{live ? 'Start an inspection' : 'See a full report'}</Button></div>
          </div>
        </section>

        <section className="wrap section" aria-labelledby="faq-h">
          <h2 id="faq-h" className="h-xl">Questions</h2>
          <div className="faq">
            <details open><summary>Is this a mechanical inspection?</summary><p>No. UpClose looks at what your camera can see. It can't hear the engine or lift the vehicle. Use it to decide which car is worth a mechanic's time, and what to ask about.</p></details>
            <details><summary>What do I need?</summary><p>A phone with a camera and about three minutes at the car in daylight. No app to install.</p></details>
            <details><summary>What if I can't film everything?</summary><p>Skip it. Anything you don't film is marked as not checked in your report. It never counts as clean.</p></details>
            <details><summary>What happens to my footage?</summary><p>It's stored privately and only you can see it. You can delete it at any time.</p></details>
            {!live && <details><summary>When can I use it?</summary><p>The app opens to the public soon. Until then you can read the sample report or run UpClose yourself from the code on GitHub.</p></details>}
          </div>
          <div className="closer">
            <h2 className="h-lg">{live ? 'Standing next to it right now?' : 'See what a report looks like.'}</h2>
            {live ? <Button href={appOrigin}>Start with the driver side</Button> : <Button href={SAMPLE_HREF}>Open the sample report</Button>}
          </div>
        </section>
      </main>

      <footer className="site-foot"><div className="wrap">
        <Wordmark small />
        <span>A visual screening of your footage, not a mechanical inspection.</span>
        <nav aria-label="Footer"><a href={repoUrl} target="_blank" rel="noopener noreferrer">GitHub</a><a href={SAMPLE_HREF}>Sample report</a></nav>
      </div></footer>
    </>
  );
}
