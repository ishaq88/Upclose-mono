import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type MouseEvent, type ReactElement, type ReactNode } from 'react';
import { checklistItems, ReportAdvice, ReportMechanical, type MediaResolver } from './ReportExtras';
import { EvidencePrint } from './EvidencePrint';
import {
  findingTitle, formatDollars, formatIndex, isSerious, lowestZone, notChecked, OPTIONAL_ZONES, orderFindings,
  scoredZones, sortedZones, verdict, ZONE_LABEL, type PaidReport, type ZoneScore,
} from './model';
import { CoverageBar, Disclosure, EvidenceFrame, PageHeader, Pill, ScoreTiles, SeverityPips, type Crumb, type ZoneCoverage } from '../ui';
import './report.css';

const TopDownMap = lazy(() => import('./TopDownMap'));

/** The page header the report carries in its sticky left column: crumbs, the vehicle label, a meta line and actions. */
export interface ReportHeader {
  crumbs?: ReadonlyArray<Crumb>;
  title: string;
  meta?: ReactNode;
  actions?: ReactNode;
}

interface Props {
  report: PaidReport;
  header?: ReportHeader;
  /** URL of the evidence frame for a finding or candidate id. */
  frameUrl?: (id: string) => string;
  /** Owner view: resolves a frame to a data URL on demand, so no token is ever put in an address. Wins over `frameUrl`. */
  resolveFrame?: (id: string) => Promise<string>;
  resolveMedia?: MediaResolver;
}

const SECTIONS = [
  ['r-found', 'What we found'], ['r-missing', 'Not checked'], ['r-mechanical', 'Mechanical checks'],
  ['r-advice', 'Advice and audio'], ['r-listing', 'The listing'], ['r-costs', 'Repair costs'], ['r-next', 'Checklist'], ['r-zones', 'All zones'],
] as const;

const VERDICT_LABEL = { contradicted: "Doesn't match", supported: 'Matches', not_visible: "Can't tell" } as const;

const NOT_CHECKED_TIP: Partial<Record<ZoneScore['zone'], string>> = {
  engine_bay: 'Ask to see it with the hood open, or film it again.',
  underbody: 'Ask a mechanic to look underneath.',
  tires: 'Check the tread depth yourself or ask for receipts.',
  dash_electrical: 'Check for warning lights with the ignition on.',
  interior: 'Look for damp carpets and torn seats.',
  front_rear_body: 'Walk both ends slowly and check the glass for cracks.',
  driver_side: 'Film the side again in good light.',
  passenger_side: 'Film the side again in good light.',
};

function useActiveSection(): string {
  const [active, setActive] = useState<string>(SECTIONS[0][0]);
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) if (entry.isIntersecting) setActive(entry.target.id);
    }, { rootMargin: '-30% 0px -60% 0px' });
    for (const [id] of SECTIONS) { const el = document.getElementById(id); if (el) observer.observe(el); }
    return () => observer.disconnect();
  }, []);
  return active;
}

/** True on a laptop-width window, so the disclosure that holds the checked evidence starts open there. */
function useWide(): boolean {
  const [wide] = useState(() => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(min-width: 1100px)').matches);
  return wide;
}

/** A chip or link that points at a collapsed disclosure opens it first, so the jump lands on content. */
function openTarget(event: MouseEvent<HTMLAnchorElement>): void {
  const id = event.currentTarget.getAttribute('href')?.slice(1);
  const el = id ? document.getElementById(id) : null;
  if (el instanceof HTMLDetailsElement) el.open = true;
}

function zoneCoverage(zone: ZoneScore): ZoneCoverage['state'] {
  if (zone.state === 'assessed' && zone.score !== null) return 'assessed';
  if (zone.state === 'partial') return 'partial';
  return OPTIONAL_ZONES.has(zone.zone) ? 'optional' : 'not_checked';
}

interface Attention { key: string; tone: 'risk' | 'claim' | 'not'; title: string; body: ReactNode }

export function ReportView({ report, header, frameUrl, resolveFrame, resolveMedia }: Props): ReactElement {
  const findings = useMemo(() => orderFindings(report.findings), [report.findings]);
  const numberOf = useMemo(() => new Map(findings.map((f, i) => [f.id, i + 1])), [findings]);
  const possible = report.candidates?.length ?? 0;
  const v = verdict(findings, possible);
  const zones = report.scores.zones;
  const lowest = lowestZone(zones);
  const scored = scoredZones(zones);
  const scorable = zones.filter((z) => !OPTIONAL_ZONES.has(z.zone)).length;
  const missing = notChecked(zones);
  const missingCore = missing.filter((z) => !OPTIONAL_ZONES.has(z.zone));
  const active = useActiveSection();
  const wide = useWide();
  const [flashed, setFlashed] = useState<string | null>(null);
  const onPin = useCallback((id: string) => { setFlashed(id); window.setTimeout(() => setFlashed(null), 1800); }, []);
  const ref = (ids: readonly string[]): ReactElement[] => ids.flatMap((id) => {
    const n = numberOf.get(id);
    return n ? [<a key={id} className="r-ref" href={`#finding-${n}`}>Finding {n}</a>] : [];
  });
  const frameProps = (id: string): { src?: string; resolve?: () => Promise<string> } =>
    resolveFrame ? { resolve: () => resolveFrame(id) } : { src: frameUrl?.(id) };
  const contradicted = report.claims.filter((c) => c.verdict === 'contradicted');
  const items = useMemo(() => checklistItems(report), [report]);
  const coverage: ZoneCoverage[] = sortedZones(zones).map((z) => ({ label: ZONE_LABEL[z.zone].replace(', glass', ' with glass'), state: zoneCoverage(z) }));

  const attention: Attention[] = [
    ...findings.filter(isSerious).map((f): Attention => ({
      key: f.id, tone: 'risk', title: findingTitle(f),
      body: <>{f.evidence_note} <a href={`#finding-${numberOf.get(f.id)}`}>See finding {numberOf.get(f.id)}</a></>,
    })),
    ...contradicted.map((c): Attention => ({
      key: `claim-${c.claim_index}`, tone: 'claim', title: `The listing says "${c.text}"`,
      body: <>Your footage shows otherwise. <a href="#r-listing" onClick={openTarget}>See the listing check</a></>,
    })),
    ...(missingCore.length > 0 ? [{
      key: 'missing', tone: 'not' as const, title: `${missingCore.map((z) => ZONE_LABEL[z.zone]).join(', ')} not fully checked`,
      body: <>They're not in the index. <a href="#r-missing">What to do</a></>,
    }] : []),
  ].slice(0, 3);

  const claimNoun = report.claims.length === 1 ? 'claim' : 'claims';
  const listingSummary = report.claims.length === 0 ? 'No listing was added'
    : `${contradicted.length} of ${report.claims.length} ${claimNoun} ${contradicted.length === 1 ? "doesn't" : "don't"} match`;
  const costsSummary = `${formatDollars(report.repairs.subtotal_low_cents)} – ${formatDollars(report.repairs.subtotal_high_cents)}`;

  return (
    <div className="rep-scope"><div className="rv">
      <aside className="rv-side">
        {header && <PageHeader crumbs={header.crumbs} title={header.title} meta={header.meta} actions={header.actions} />}
        <section className="rsec rv-summary" id="r-summary" aria-labelledby="summary-h">
          <h2 id="summary-h" className="sr-only">Summary</h2>
          <div className="verdict">
            <span className={`k-ico ${v.serious ? 'k-risk' : 'k-ok'}`} aria-hidden="true" />
            <div><h3>{v.headline}</h3><p>{v.advice}</p></div>
          </div>
          {report.categories
            ? <ScoreTiles categories={report.categories} />
            : (
              <div className="rv-index">
                <div className="label">Condition index</div>
                {report.scores.headline === null
                  ? <div className="idx-none">Not enough zones checked for an overall index</div>
                  : <div className="idx"><span className="idx-n num">{formatIndex(report.scores.headline)}</span><span className="muted">/ 100</span></div>}
              </div>
            )}
          {lowest && lowest.score !== null && <p className="label">Lowest zone: <b className="num">{ZONE_LABEL[lowest.zone]} {formatIndex(lowest.score)}</b></p>}
          <CoverageBar zones={coverage} />
          <p className="muted small">It measures visible damage, not whether the car is a good buy.</p>
          <div className="rv-attention">
            <h3>What needs attention</h3>
            <p className="muted small">Worth raising before a price is agreed.</p>
            {attention.length === 0
              ? <p className="small">No serious findings or listing mismatches. Check what wasn't checked below.</p>
              : (
                <ul className="know">
                  {attention.map((row) => (
                    <li key={row.key} className={row.tone === 'risk' ? 'know-risk' : undefined}>
                      <span className={`k-ico ${row.tone === 'risk' ? 'k-risk' : row.tone === 'claim' ? 'k-claim' : 'k-not'}`} aria-hidden="true" />
                      <div><strong>{row.title}</strong><span>{row.body}</span></div>
                    </li>
                  ))}
                </ul>
              )}
          </div>
        </section>
      </aside>

      <div className="rv-main">
        <nav className="rv-chips" aria-label="Report sections">
          {SECTIONS.map(([id, label]) => <a key={id} href={`#${id}`} onClick={openTarget} className={active === id ? 'on' : undefined}>{label}</a>)}
        </nav>

        <section className="rsec" id="r-found" aria-labelledby="found-h">
          <h2 id="found-h">What we found</h2>
          <p className="sub">{findings.length} confirmed {findings.length === 1 ? 'finding' : 'findings'}{possible ? `, ${possible} possible to check yourself` : ''}, most serious first. Tap a frame to enlarge.</p>
          <div className="prints">
            {findings.map((f, i) => {
              const n = i + 1, id = `finding-${n}`, title = findingTitle(f);
              return (
                <EvidencePrint key={f.id} id={id} highlighted={flashed === id}
                  frame={f.evidence_available ? <EvidenceFrame {...frameProps(f.id)} className="ev-card-frame" bbox={f.bbox} timestampMs={f.source_timestamp_ms} label={title} /> : <div className="ev-frame ev-card-frame ev-missing">No image shared</div>}>
                  <div className="ev-meta">
                    {isSerious(f) && <Pill tone="risk">Serious</Pill>}
                    <span className="sev"><SeverityPips severity={f.severity} />Severity {f.severity} of 5</span>
                  </div>
                  <h3>{n} · {title}</h3>
                  <p className="muted ev-note">{f.evidence_note}</p>
                </EvidencePrint>
              );
            })}
          </div>
          {report.candidates?.map((c) => (
            <div key={c.id} className="candidate">
              {c.evidence_available && <EvidenceFrame className="ev-thumb" {...frameProps(c.id)} bbox={c.bbox} timestampMs={c.source_timestamp_ms} label={findingTitle(c)} />}
              <p><b>Possible {findingTitle(c).toLowerCase()}.</b> <span className="muted">We weren't sure, so it isn't scored. Have a look yourself.</span></p>
            </div>
          ))}
          <div className="map-col">
            <Suspense fallback={<div className="map"><span className="map-status">Loading map…</span></div>}>
              <TopDownMap findings={findings} zones={zones} onPin={onPin} />
            </Suspense>
            <div className="map-key"><span><i className="sw sw-checked" />Checked</span><span><i className="sw sw-serious" />Serious</span><span><i className="sw sw-minor" />Minor</span></div>
          </div>
        </section>

        <section className="rsec" id="r-missing" aria-labelledby="missing-h">
          <h2 id="missing-h">What we couldn't check</h2>
          <p className="sub">Not checked doesn't mean clean. These aren't in the index.</p>
          <div className="missing">
            {missing.map((z) => (
              <div key={z.zone} className="miss">
                <strong>{ZONE_LABEL[z.zone]}{OPTIONAL_ZONES.has(z.zone) ? <Pill tone="optional">Optional</Pill> : <Pill tone="not">Not checked</Pill>}</strong>
                <span className="muted">{z.state === 'partial' ? `Partly checked. ${z.reason ?? ''}` : z.reason ?? 'Not checked.'}</span>
                <span>{NOT_CHECKED_TIP[z.zone]}</span>
              </div>
            ))}
          </div>
        </section>

        <div className="rv-stack">
          <ReportMechanical key={`m-${report.revision_id}`} report={report} resolveMedia={resolveMedia} defaultOpen={wide} />
          <ReportAdvice key={`a-${report.revision_id}`} report={report} resolveMedia={resolveMedia} />

          <Disclosure id="r-listing" title="Does the listing hold up?" headingLevel={2} summary={listingSummary}>
            <p className="muted">What the seller wrote, checked against your footage.</p>
            {report.claims.length === 0 ? <p>No listing was added, so there was nothing to check.</p> : (
              <div className="tbl"><table className="stack">
                <thead><tr><th scope="col">Seller said</th><th scope="col">Why</th><th scope="col" className="r">Result</th></tr></thead>
                <tbody>{report.claims.map((c) => (
                  <tr key={c.claim_index}><th scope="row">"{c.text}"</th><td>{c.rationale} {ref(c.finding_ids)}</td>
                    <td className="r"><span className={`verdict-tag v-${c.verdict}`}>{VERDICT_LABEL[c.verdict]}</span></td></tr>
                ))}</tbody>
              </table></div>
            )}
          </Disclosure>

          <Disclosure id="r-costs" title="What repairs might cost" headingLevel={2} summary={costsSummary}>
            <p className="muted">Typical ranges to help you negotiate. Get a quote before you rely on them.</p>
            <div className="tbl"><table className="stack">
              <thead><tr><th scope="col">Repair</th><th scope="col">For</th><th scope="col" className="r">Range</th></tr></thead>
              <tbody>{report.repairs.items.map((item, i) => {
                const f = report.findings.find((x) => x.id === item.finding_ids[0]);
                const what = f ? findingTitle(f) : 'Repair';
                return (
                  <tr key={i}><th scope="row" className={item.low_cents === null ? 'muted' : undefined}>{what}</th><td>{ref(item.finding_ids)}</td>
                    <td className="r">{item.low_cents !== null && item.high_cents !== null ? <b className="num">{formatDollars(item.low_cents)} – {formatDollars(item.high_cents)}</b> : <span className="muted">Ask a body shop</span>}</td></tr>
                );
              })}</tbody>
              <tfoot><tr><th scope="row" colSpan={2}>Estimated total</th><td className="r"><b className="num total">{costsSummary}</b></td></tr></tfoot>
            </table></div>
          </Disclosure>

          <Disclosure id="r-next" title="Your checklist" headingLevel={2} summary={`${items.length} ${items.length === 1 ? 'item' : 'items'} for the seller and your mechanic`}>
            <p className="muted">Take this to the seller and your mechanic.</p>
            <ul className="checklist">
              {items.map((row, i) => (
                <li key={i}><label><input type="checkbox" /><span>{row.text} {ref(row.findingIds)}<span className="muted check-src">{row.source}</span></span></label></li>
              ))}
            </ul>
          </Disclosure>

          <Disclosure id="r-zones" title="All zones" headingLevel={2} summary={`${scored.length} of ${scorable} zones scored`}>
            <div className="tbl"><table className="stack">
              <thead><tr><th scope="col">Zone</th><th scope="col">Findings</th><th scope="col" className="r">Index</th></tr></thead>
              <tbody>{sortedZones(zones).map((z) => {
                const zoneFindings = findings.filter((f) => f.zone === z.zone);
                const scoredZone = z.state === 'assessed' && z.score !== null;
                return (
                  <tr key={z.zone} className={scoredZone ? undefined : 'not'}>
                    <th scope="row">{ZONE_LABEL[z.zone]}</th>
                    <td>{scoredZone ? (zoneFindings.length ? ref(zoneFindings.map((f) => f.id)) : <span className="muted">Nothing found</span>)
                      : <span className="muted">{OPTIONAL_ZONES.has(z.zone) ? 'Optional, not scored' : z.state === 'partial' ? `Partly checked, so no score. ${z.reason ?? ''}` : `Not checked. ${z.reason ?? ''}`}</span>}</td>
                    <td className="r">{scoredZone ? <b className="num">{formatIndex(z.score!)}</b> : <span className="muted">–</span>}</td>
                  </tr>
                );
              })}</tbody>
            </table></div>
          </Disclosure>

          <Disclosure id="r-about" title="About this report" headingLevel={2}>
            <p className="muted small">UpClose screens what's visible in your footage. It isn't a mechanical inspection and doesn't replace one. The index only covers zones we fully checked; model confidence isn't an accuracy measure. {report.repairs.label}.</p>
          </Disclosure>
        </div>
      </div>
    </div></div>
  );
}
