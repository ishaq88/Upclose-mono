import { useMemo, useSyncExternalStore, type ReactElement, type ReactNode } from 'react';
import { createApi } from '../../api/client';
import type { CategoryScores } from '../../api/client';
import { CaptureFlow } from '../../components/CaptureFlow';
import { ConsentForm } from '../../components/ConsentForm';
import { Part1Slot } from '../../components/FreeVerdictCard';
import { ReportView } from '../../report/ReportView';
import { SAMPLE_REPORT } from '../../sample/sample';
import { FakeBackend, syntheticVerdict } from '../../testing/fakeBackend';
import {
  ActionBar, Banner, Button, Card, ChoiceTiles, CoverageBar, Disclosure, EvidenceFrame, Field, Notice, PageHeader, Pill, ScoreTiles,
  Section, SeverityPips, StatusChip, type PillTone, type ZoneCoverage,
} from '../index';
import './gallery.css';

/** Dev-only. Screen states run on the in-memory FakeBackend: no network, no paid calls, nothing is uploaded. */
export const STATES = [
  ['start', 'Start'],
  ['capture-fresh', 'Capture, fresh'],
  ['capture-mid', 'Capture, 2 filmed, 1 processing, free result ready'],
  ['capture-engine', 'Capture, engine-check step'],
  ['wrapup', 'Wrap-up'],
  ['finished', 'Finished, progress'],
  ['free-result', 'Free result'],
  ['paid-report', 'Paid report (sample)'],
] as const;

type StateId = (typeof STATES)[number][0];

function subscribe(listener: () => void): () => void {
  window.addEventListener('hashchange', listener);
  return () => window.removeEventListener('hashchange', listener);
}

function useHash(): string {
  return useSyncExternalStore(subscribe, () => window.location.hash.replace(/^#/, ''), () => '');
}

function backendFor(id: StateId): FakeBackend {
  const backend = new FakeBackend();
  backend.sessionState = 'capturing';
  if (id === 'capture-mid' || id === 'wrapup') {
    backend.parts[0]!.state = 'processed';
    backend.parts[1]!.state = 'processed';
    backend.parts[2]!.state = 'processing';
    backend.freeVerdict = syntheticVerdict();
  }
  if (id === 'finished') {
    backend.parts.forEach((part, index) => { part.state = index < 3 ? 'processed' : 'skipped'; });
    backend.sessionState = 'finalized';
    backend.freeVerdict = syntheticVerdict();
    backend.report = { slug: 'slug-abc', state: 'building', unlocked: false };
  }
  return backend;
}

function useFake(id: StateId): { backend: FakeBackend; api: ReturnType<typeof createApi> } {
  return useMemo(() => {
    const backend = backendFor(id);
    const api = createApi({ getToken: () => backend.token, baseUrl: '/api/v1', fetchImpl: (input, init) => backend.fetch(input, init as RequestInit) });
    return { backend, api };
  }, [id]);
}

function CaptureState({ id }: { id: StateId }): ReactElement {
  const { backend, api } = useFake(id);
  return <CaptureFlow api={api} sessionId={backend.sessionId} ownerToken={backend.token} onNewSession={() => undefined} />;
}

function StartState(): ReactElement {
  const { api } = useFake('start');
  return <ConsentForm api={api} onCreated={() => undefined} />;
}

/** Sample frames are served by the landing build; the app's dev server has none, so a local landing server supplies them. */
const frameUrl = (frameId: string): string => `http://127.0.0.1:5181/sample/frames/${frameId}.jpg`;

function Screen({ id }: { id: StateId }): ReactElement {
  const shell = (children: ReactNode): ReactElement => <main className="wrap app-main">{children}</main>;
  switch (id) {
    case 'start': return shell(<StartState />);
    case 'capture-fresh':
    case 'capture-engine':
    case 'capture-mid':
    case 'wrapup':
    case 'finished': return shell(<CaptureState key={id} id={id} />);
    case 'free-result': return shell(<Part1Slot part1={undefined} verdict={syntheticVerdict()} />);
    case 'paid-report': return shell(<ReportView report={SAMPLE_REPORT} frameUrl={frameUrl} />);
  }
}

const PILL_TONES: PillTone[] = ['filmed', 'ok', 'not', 'warn', 'risk', 'optional', 'synthetic'];

const CATEGORIES = {
  visual: { state: 'assessed', score: 94.96, reason: null },
  mechanical: { state: 'not_checked', score: null, reason: null },
  overall: { state: 'not_checked', score: null, basis: 'visual_only', note: '' },
} as unknown as CategoryScores;

const ZONES: ZoneCoverage[] = [
  { label: 'Driver side', state: 'assessed' }, { label: 'Passenger side', state: 'assessed' }, { label: 'Front and rear', state: 'partial' },
  { label: 'Tires', state: 'assessed' }, { label: 'Engine bay', state: 'not_checked' }, { label: 'Underbody', state: 'not_checked' },
  { label: 'Dashboard', state: 'optional' }, { label: 'Interior', state: 'optional' },
];

function Group({ title, children }: { title: string; children: ReactNode }): ReactElement {
  return <section className="gal-group"><h2 className="gal-h">{title}</h2><div className="gal-row">{children}</div></section>;
}

function KitGallery(): ReactElement {
  return (
    <main className="wrap app-main gal">
      <PageHeader crumbs={[{ label: 'Dev', href: '/__gallery' }, { label: 'Kit' }]} title="UI kit gallery" meta="Development only. Screen states below use the in-memory fake backend." />
      <Group title="Screen states">
        {STATES.map(([id, label]) => <Button key={id} variant="secondary" size="sm" href={`#${id}`} className="gal-state">{label}</Button>)}
      </Group>
      <Group title="Type scale">
        <div className="gal-type">
          <p className="gal-display">Display 40 to 64</p>
          <h1 className="page-title">Heading 1</h1>
          <h2 className="section-title">Heading 2</h2>
          <h3 className="disc-title">Heading 3</h3>
          <p>Body 17. Look closer before you buy.</p>
          <p className="hint">Small 15. Fine print follows.</p>
          <p className="gal-fine">Fine 13</p>
          <p className="gal-label">Label 12</p>
          <p className="num gal-num">94.9 0123456789</p>
        </div>
      </Group>
      <Group title="Button: variants">
        <Button>Primary</Button><Button variant="secondary">Secondary</Button><Button variant="dark">Dark</Button><Button variant="danger">Danger</Button><Button variant="text">Text</Button>
      </Group>
      <Group title="Button: sizes, block, link, icon, disabled">
        <Button size="sm">Small</Button><Button href="#kit" variant="secondary">As link</Button><Button icon={<span>+</span>}>With icon</Button>
        <Button disabled>Disabled</Button><Button disabledReason="Waiting for 1 clip to start" variant="dark">Waiting for 1 clip to start</Button>
        <div className="gal-wide"><Button block>Block</Button></div>
      </Group>
      <Group title="Card">
        <Card>Default</Card><Card tone="sunk" padding="lg">Sunk, large</Card><Card tone="danger-outline" padding="md">Danger outline</Card>
      </Group>
      <Group title="Pill">
        {PILL_TONES.map((tone) => <Pill key={tone} tone={tone}>{tone === 'not' ? 'Not filmed' : tone}</Pill>)}
      </Group>
      <Group title="Notice">
        <Notice tone="info" title="Info">Plain information.</Notice>
        <Notice tone="warn" title="Warn" action={<Button variant="text" size="sm">Do something</Button>}>Something to check.</Notice>
        <Notice tone="error" title="Error">That did not work.</Notice>
      </Group>
      <Group title="Field">
        <Field label="Email" hint="We never share it"><input type="email" defaultValue="" /></Field>
        <Field label="Password" error="Use at least 12 characters"><input type="password" defaultValue="short" /></Field>
        <Field label="Listing"><textarea rows={2} defaultValue="" /></Field>
      </Group>
      <Group title="ChoiceTiles">
        <ChoiceTiles name="gal-mode" legend="Capture mode" value="a" onChange={() => undefined}
          options={[{ value: 'a', title: 'One video', description: 'One walk around the car.', tag: 'Quickest' }, { value: 'b', title: 'Guided clips', description: 'A short clip per zone.', tag: 'Most thorough', tagTone: 'quiet' }]} />
      </Group>
      <Group title="Disclosure">
        <Disclosure title="Mechanical checks" summary="Not checked" headingLevel={2}>Body text.</Disclosure>
        <Disclosure title="The listing" summary="1 of 2 claims doesn't match" defaultOpen>Open by default.</Disclosure>
      </Group>
      <Group title="StatusChip and Banner">
        <StatusChip filmed={2} total={8} checking={1} /><StatusChip filmed={8} total={8} />
        <Banner action={<Button variant="text" size="sm">View</Button>}>Driver side result is ready</Banner>
      </Group>
      <Group title="ScoreTiles">
        <ScoreTiles categories={CATEGORIES} conditionIndex={89.99} />
      </Group>
      <Group title="CoverageBar">
        <CoverageBar zones={ZONES} />
      </Group>
      <Group title="SeverityPips and EvidenceFrame">
        <SeverityPips severity={3} />
        <div className="gal-frame"><EvidenceFrame src={frameUrl('3f6c1d2a-5b7e-4c1a-9d2e-1a2b3c4d5e01')} bbox={{ x: 0.5, y: 0.5, w: 0.2, h: 0.2 }} timestampMs={65000} label="Sample frame (synthetic)" /></div>
      </Group>
      <Group title="Section">
        <Section title="Section title" sub="With a sub line"><p>Content.</p></Section>
      </Group>
      <Group title="ActionBar">
        <ActionBar label="Example actions"><Button variant="dark">Finish and build report</Button><Button variant="secondary">Keep filming</Button></ActionBar>
      </Group>
    </main>
  );
}

/** `/__gallery` shows the kit; `/__gallery#<state>` shows one screen state on its own, as the real page. */
export default function Gallery(): ReactElement {
  const hash = useHash();
  const state = STATES.find(([id]) => id === hash);
  return state ? <Screen id={state[0]} /> : <KitGallery />;
}
