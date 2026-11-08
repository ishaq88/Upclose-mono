import { useMemo, useState, type ReactElement, type ReactNode } from 'react';
import {
  DashboardGroup, EngineGroup, NarrationPlayer, notRecordedSources, ReportAdvice, SoundGroup, type MediaResolver,
} from './ReportExtras';
import { EvidencePrint } from './EvidencePrint';
import {
  findingTitle, formatDollars, formatIndex, isSerious, OPTIONAL_ZONES, orderFindings,
  scoredZones, sortedZones, verdict, ZONE_LABEL, type PaidReport, type ZoneScore,
} from './model';
import { Button, EvidenceFrame, PageHeader, Pill, ScoreBand, SeverityPips, type Crumb } from '../ui';
import './report.css';

/** The page header: crumbs, the vehicle label, a meta line, and the compact action row (Share, Listen, Rename). */
export interface ReportHeader {
  crumbs?: ReadonlyArray<Crumb>;
  title: string;
  meta?: ReactNode;
  share?: ReactNode;
  rename?: ReactNode;
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

export const NO_VISIBLE_FINDINGS = 'No visible findings detected in assessed footage';

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

function Mark({ kind }: { kind: 'ok' | 'part' | 'no' }): ReactElement {
  const glyph = kind === 'ok' ? '✓' : kind === 'part' ? '◐' : '✕';
  const word = kind === 'ok' ? 'Assessed' : kind === 'part' ? 'Partly checked' : 'Not recorded';
  return <span className={`cov-mark cov-${kind}`}><span aria-hidden="true">{glyph}</span><span className="sr-only">{word}: </span></span>;
}

export function ReportView({ report, header, frameUrl, resolveFrame, resolveMedia }: Props): ReactElement {
  const findings = useMemo(() => orderFindings(report.findings), [report.findings]);
  const possible = report.candidates?.length ?? 0;
  const v = verdict(findings, possible);
  const zones = report.scores.zones;
  const scored = scoredZones(zones);
  const [listening, setListening] = useState(false);
  const frameProps = (id: string): { src?: string; resolve?: () => Promise<string> } =>
    resolveFrame ? { resolve: () => resolveFrame(id) } : { src: frameUrl?.(id) };
  const contradicted = report.claims.filter((c) => c.verdict === 'contradicted');
  const visualRecorded = findings.length > 0 || possible > 0 || zones.some((z) => z.state === 'assessed' || z.state === 'partial');
  const estimates = report.repairs.items.some((item) => item.low_cents !== null);
  const costs = `${formatDollars(report.repairs.subtotal_low_cents)} – ${formatDollars(report.repairs.subtotal_high_cents)}`;
  const advisory = report.advisory;
  const missingSources = notRecordedSources(report);

  return (
    <div className="rep-scope"><div className="rv">
      {header && (
        <PageHeader crumbs={header.crumbs} title={header.title} meta={header.meta}
          actions={<>{header.share}{report.narration_audio && <Button variant="secondary" size="sm" aria-expanded={listening} onClick={() => setListening((x) => !x)}>Listen</Button>}{header.rename}</>} />
      )}
      {listening && <NarrationPlayer report={report} resolveMedia={resolveMedia} />}

      <ScoreBand categories={report.categories} headline={report.scores.headline} zonesChecked={scored.length} />

      <section className="rsec rv-verdict" id="r-summary" aria-labelledby="summary-h">
        <h2 id="summary-h" className="sr-only">Summary</h2>
        <div className="verdict">
          <span className={`k-ico ${v.serious ? 'k-risk' : 'k-neutral'}`} aria-hidden="true" />
          <div><h3>{v.headline}</h3><p>{v.advice}</p></div>
        </div>
      </section>

      <section className="rsec" id="r-found" aria-labelledby="found-h">
        <h2 id="found-h">What we found</h2>
        {visualRecorded && (
          <div className="group" data-testid="group-visual">
            <h3 className="group-title">Visual</h3>
            {findings.length === 0 && possible === 0 && <p>{NO_VISIBLE_FINDINGS}</p>}
            <div className="prints">
              {findings.map((f, i) => {
                const n = i + 1, title = findingTitle(f);
                return (
                  <EvidencePrint key={f.id} id={`finding-${n}`}
                    frame={f.evidence_available ? <EvidenceFrame {...frameProps(f.id)} className="ev-card-frame" bbox={f.bbox} timestampMs={f.source_timestamp_ms} label={title} /> : <div className="ev-frame ev-card-frame ev-missing">No image shared</div>}>
                    <div className="ev-meta">
                      {isSerious(f) && <Pill tone="risk">Serious</Pill>}
                      <span className="sev"><SeverityPips severity={f.severity} />Severity {f.severity} of 5</span>
                    </div>
                    <h4>{n} · {title}</h4>
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
          </div>
        )}
        <EngineGroup report={report} />
        <SoundGroup report={report} resolveMedia={resolveMedia} />
        <DashboardGroup report={report} />
        {report.claims.length > 0 && (
          <div className="group" data-testid="group-listing">
            <h3 className="group-title">Listing</h3>
            {contradicted.length === 0 && <p>No listing claim is contradicted by the footage.</p>}
            {contradicted.map((c) => <p key={c.claim_index} className="fault"><b>The listing says "{c.text}".</b> {c.rationale}</p>)}
          </div>
        )}
      </section>

      <ReportAdvice report={report} />

      {estimates && (
        <section className="rsec" id="r-costs" aria-labelledby="costs-h">
          <h2 id="costs-h">What repairs might cost</h2>
          <div className="tbl"><table>
            <thead><tr><th scope="col">Repair</th><th scope="col" className="r">Range</th></tr></thead>
            <tbody>{report.repairs.items.map((item, i) => {
              const f = report.findings.find((x) => x.id === item.finding_ids[0]);
              return (
                <tr key={i}><th scope="row" className={item.low_cents === null ? 'muted' : undefined}>{f ? findingTitle(f) : 'Repair'}</th>
                  <td className="r">{item.low_cents !== null && item.high_cents !== null ? <b className="num">{formatDollars(item.low_cents)} – {formatDollars(item.high_cents)}</b> : <span className="muted">Ask a body shop</span>}</td></tr>
              );
            })}</tbody>
            <tfoot><tr><th scope="row">Estimated total</th><td className="r"><b className="num total">{costs}</b></td></tr></tfoot>
          </table></div>
          <p className="muted small">{report.repairs.label}</p>
        </section>
      )}

      <section className="rsec" id="r-coverage" aria-labelledby="coverage-h">
        <h2 id="coverage-h">Coverage and what wasn't recorded</h2>
        <ul className="cov">
          {sortedZones(zones).map((z) => {
            const ok = z.state === 'assessed' && z.score !== null;
            const optional = OPTIONAL_ZONES.has(z.zone);
            return (
              <li key={z.zone}>
                <Mark kind={ok ? 'ok' : z.state === 'partial' ? 'part' : 'no'} />
                <span><b>{ZONE_LABEL[z.zone]}</b>{optional && <> <Pill tone="optional">Optional</Pill></>}{' '}
                  {ok ? <span className="num">{formatIndex(z.score!)}</span>
                    : <span className="muted">{z.state === 'partial' ? `Partly checked${z.reason ? `: ${z.reason}` : ''}` : (z.reason ?? 'Not recorded')}{!optional && NOT_CHECKED_TIP[z.zone] ? `. ${NOT_CHECKED_TIP[z.zone]}` : ''}</span>}
                </span>
              </li>
            );
          })}
          {missingSources.map((row) => (
            <li key={row.key}><Mark kind="no" /><span><b>{row.label}</b> <span className="muted">not recorded{row.reason ? `: ${row.reason}` : ''}</span></span></li>
          ))}
          {report.claims.length === 0 && <li><Mark kind="no" /><span><b>Listing</b> <span className="muted">not added</span></span></li>}
          {(!advisory || advisory.status !== 'available') && (
            <li><Mark kind="no" /><span><b>AI advice</b> <span className="muted">not available{advisory?.reason ? `: ${advisory.reason.replaceAll('_', ' ')}` : missingSources.length ? ': needs engine evidence' : ''}</span></span></li>
          )}
        </ul>
        <p className="muted small">Not checked doesn't mean clean.</p>
      </section>

      <p className="muted small rv-about">About this report: UpClose screens what's visible in your footage. It isn't a mechanical inspection. The index only covers zones we fully checked, and model confidence isn't an accuracy measure.</p>
    </div></div>
  );
}
