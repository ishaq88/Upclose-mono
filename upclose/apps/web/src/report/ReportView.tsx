import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import { CategoryScores } from './CategoryScores';
import { ReportExtras, type MediaResolver } from './ReportExtras';
import { EvidenceFrame, EvidencePrint, SeverityPips } from './EvidencePrint';
import {
  findingTitle, formatDollars, formatIndex, isSerious, lowestZone, notChecked, OPTIONAL_ZONES, orderFindings,
  scoredZones, sortedZones, verdict, ZONE_LABEL, type PaidReport, type ZoneScore,
} from './model';
import './report.css';

const TopDownMap = lazy(() => import('./TopDownMap'));

interface Props {
  report: PaidReport;
  /** URL of the evidence frame for a finding or candidate id. */
  frameUrl?: (id: string) => string;
  /** Owner view: resolves a frame to a data URL on demand, so no token is ever put in an address. Wins over `frameUrl`. */
  resolveFrame?: (id: string) => Promise<string>;
  resolveMedia?: MediaResolver;
}

const SECTIONS = [
  ['r-summary', 'Summary'], ['r-found', 'What we found'], ['r-missing', 'Not checked'], ['r-listing', 'The listing'],
  ['r-mechanical', 'Mechanical checks'], ['r-costs', 'Repair costs'], ['r-next', 'Checklist'], ['r-zones', 'All zones'],
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
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) if (entry.isIntersecting) setActive(entry.target.id);
    }, { rootMargin: '-30% 0px -60% 0px' });
    for (const [id] of SECTIONS) { const el = document.getElementById(id); if (el) observer.observe(el); }
    return () => observer.disconnect();
  }, []);
  return active;
}

export function ReportView({ report, frameUrl, resolveFrame, resolveMedia }: Props): ReactElement {
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
  const [flashed, setFlashed] = useState<string | null>(null);
  const onPin = useCallback((id: string) => { setFlashed(id); window.setTimeout(() => setFlashed(null), 1800); }, []);
  const ref = (ids: readonly string[]): ReactElement[] => ids.flatMap((id) => {
    const n = numberOf.get(id);
    return n ? [<a key={id} className="r-ref" href={`#finding-${n}`}>Finding {n}</a>] : [];
  });
  const frameProps = (id: string): { src?: string; resolve?: () => Promise<string> } =>
    resolveFrame ? { resolve: () => resolveFrame(id) } : { src: frameUrl?.(id) };
  const contradicted = report.claims.filter((c) => c.verdict === 'contradicted');

  return (
    <div className="rep-scope"><div className="rep">
      <nav className="toc" aria-label="Report sections">
        {SECTIONS.map(([id, label]) => <a key={id} href={`#${id}`} className={active === id ? 'on' : undefined}>{label}</a>)}
      </nav>
      <div className="rep-body">
        <section className="rsec" id="r-summary" aria-labelledby="summary-h">
          <h2 id="summary-h" className="sr-only">Summary</h2>
          <div className="summary">
            <div className="summary-main">
              <div className="verdict">
                <span className={`k-ico ${v.serious ? 'k-risk' : 'k-ok'}`} aria-hidden="true" />
                <div><h3>{v.headline}</h3><p>{v.advice}</p></div>
              </div>
              <div className="summary-nums">
                <div>
                  <div className="label">Condition index</div>
                  {report.scores.headline === null
                    ? <div className="idx-none">Not enough zones checked for an overall index</div>
                    : <div className="idx"><span className="idx-n num">{formatIndex(report.scores.headline)}</span><span className="muted">/ 100</span></div>}
                </div>
                {lowest && lowest.score !== null && (
                  <div><div className="label">Lowest zone</div><div className="idx-zone">{ZONE_LABEL[lowest.zone]} <span className="num">{formatIndex(lowest.score)}</span></div></div>
                )}
              </div>
              {report.categories && <CategoryScores categories={report.categories} />}
              <p className="muted small">Based on the {scored.length} of {scorable} zones we fully checked. It measures how much visible damage we found, not whether the vehicle is a good buy.</p>
            </div>
            <div className="summary-side">
              <h3>Things to know before you buy</h3>
              <ul className="know">
                {findings.filter(isSerious).map((f) => (
                  <li key={f.id} className="know-risk"><span className="k-ico k-risk" aria-hidden="true" />
                    <div><strong>{findingTitle(f)}</strong><span>{f.evidence_note} <a href={`#finding-${numberOf.get(f.id)}`}>See finding {numberOf.get(f.id)}</a></span></div></li>
                ))}
                {contradicted.map((c) => (
                  <li key={c.claim_index}><span className="k-ico k-claim" aria-hidden="true" />
                    <div><strong>The listing says "{c.text}"</strong><span>Your footage shows otherwise. <a href="#r-listing">See the listing check</a></span></div></li>
                ))}
                {missingCore.length > 0 && (
                  <li><span className="k-ico k-not" aria-hidden="true" />
                    <div><strong>{missingCore.map((z) => ZONE_LABEL[z.zone]).join(', ')} not fully checked</strong><span>They're not in the index. <a href="#r-missing">What to do</a></span></div></li>
                )}
              </ul>
            </div>
          </div>
        </section>

        <ReportExtras key={report.revision_id} report={report} resolveMedia={resolveMedia} />

        <section className="rsec" id="r-found" aria-labelledby="found-h">
          <h2 id="found-h">What we found</h2>
          <p className="sub">{findings.length} confirmed {findings.length === 1 ? 'finding' : 'findings'}{possible ? `, ${possible} possible ${possible === 1 ? 'issue' : 'issues'} to check yourself` : ''}, most serious first. Tap a frame to see it larger.</p>
          <div className="found">
            <div className="map-col">
              <Suspense fallback={<div className="map"><span className="map-status">Loading map…</span></div>}>
                <TopDownMap findings={findings} zones={zones} onPin={onPin} />
              </Suspense>
              <div className="map-key"><span><i className="sw sw-checked" />Checked</span><span><i className="sw sw-serious" />Serious</span><span><i className="sw sw-minor" />Minor</span></div>
            </div>
            <div className="prints">
              {findings.map((f, i) => {
                const n = i + 1, id = `finding-${n}`, title = findingTitle(f);
                return (
                  <EvidencePrint key={f.id} id={id} highlighted={flashed === id}
                    frame={f.evidence_available ? <EvidenceFrame {...frameProps(f.id)} bbox={f.bbox} timestampMs={f.source_timestamp_ms} label={title} /> : <div className="ev-frame ev-missing">No image shared</div>}>
                    <div className="ev-meta">
                      {isSerious(f) && <span className="risk-tag">Serious</span>}
                      <span className="sev"><SeverityPips severity={f.severity} />Severity {f.severity} of 5</span>
                    </div>
                    <h3>{n} · {title}</h3>
                    <p className="muted">{f.evidence_note}</p>
                  </EvidencePrint>
                );
              })}
            </div>
          </div>
          {report.candidates?.map((c) => (
            <div key={c.id} className="candidate">
              {c.evidence_available && <EvidenceFrame className="ev-thumb" {...frameProps(c.id)} bbox={c.bbox} timestampMs={c.source_timestamp_ms} label={findingTitle(c)} />}
              <p><b>Possible {findingTitle(c).toLowerCase()}.</b> <span className="muted">We weren't sure, so it isn't scored. Have a look yourself.</span></p>
            </div>
          ))}
        </section>

        <section className="rsec" id="r-missing" aria-labelledby="missing-h">
          <h2 id="missing-h">What we couldn't check</h2>
          <p className="sub">Not checked doesn't mean clean. These aren't in the index.</p>
          <div className="missing">
            {missing.map((z) => (
              <div key={z.zone} className="miss">
                <strong>{ZONE_LABEL[z.zone]}{OPTIONAL_ZONES.has(z.zone) && <span className="opt">Optional</span>}</strong>
                <span className="muted">{z.state === 'partial' ? `Partly checked. ${z.reason ?? ''}` : z.reason ?? 'Not checked.'}</span>
                <span>{NOT_CHECKED_TIP[z.zone]}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="rsec" id="r-listing" aria-labelledby="listing-h">
          <h2 id="listing-h">Does the listing hold up?</h2>
          <p className="sub">What the seller wrote, checked against your footage.</p>
          {report.claims.length === 0 ? <p>No listing was added, so there was nothing to check.</p> : (
            <div className="tbl"><table className="stack">
              <thead><tr><th scope="col">Seller said</th><th scope="col">Why</th><th scope="col" className="r">Result</th></tr></thead>
              <tbody>{report.claims.map((c) => (
                <tr key={c.claim_index}><th scope="row">"{c.text}"</th><td>{c.rationale} {ref(c.finding_ids)}</td>
                  <td className="r"><span className={`verdict-tag v-${c.verdict}`}>{VERDICT_LABEL[c.verdict]}</span></td></tr>
              ))}</tbody>
            </table></div>
          )}
        </section>

        <section className="rsec" id="r-costs" aria-labelledby="costs-h">
          <h2 id="costs-h">What repairs might cost</h2>
          <p className="sub">Typical ranges to help you negotiate. Get a quote before you rely on them.</p>
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
            <tfoot><tr><th scope="row" colSpan={2}>Estimated total</th><td className="r"><b className="num total">{formatDollars(report.repairs.subtotal_low_cents)} – {formatDollars(report.repairs.subtotal_high_cents)}</b></td></tr></tfoot>
          </table></div>
        </section>

        <section className="rsec" id="r-next" aria-labelledby="next-h">
          <h2 id="next-h">Your checklist</h2>
          <p className="sub">Take this to the seller and your mechanic.</p>
          <ul className="checklist">
            {report.narration.map((row, i) => (
              <li key={i}><label><input type="checkbox" /><span>{row.text} {ref(row.finding_ids)}</span></label></li>
            ))}
          </ul>
        </section>

        <section className="rsec" id="r-zones" aria-labelledby="zones-h">
          <h2 id="zones-h">All zones</h2>
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
          <details className="about"><summary>About this report</summary>
            <p>UpClose screens what's visible in your footage. It isn't a mechanical inspection and doesn't replace one. The index only covers zones we fully checked; model confidence isn't an accuracy measure. {report.repairs.label}.</p>
          </details>
        </section>
      </div>
    </div></div>
  );
}
