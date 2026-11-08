import { useEffect, useState, type ReactElement } from 'react';
import type { PaidReport, ReportMedia } from '../api/client';
import { Pill } from '../ui';

export type MediaResolver = (which: ReportMedia, signal: AbortSignal) => Promise<Blob>;
const words = (text: string): string => text.replaceAll('_', ' ');
const provenance = (value: string): string => value === 'synthetic' || value === 'fixture' ? 'Synthetic sample data' : value === 'cached' ? 'Cached recording' : 'Live';
const isSim = (value: string): boolean => value === 'synthetic' || value === 'fixture';
/** Plain words for an advisory citation such as "obd_snapshot:mil_on"; the part before the colon names the source. Unknown sources return null and are never shown raw. */
const CITE_SOURCE: Record<string, string> = {
  rig_telemetry: 'From the sensor pod', obd_snapshot: 'From the OBD snapshot', engine_audio: 'From the engine audio',
  dashboard: 'From the dashboard', video: 'From the video',
};
export function citeLabels(cites: readonly string[]): string[] {
  const out: string[] = [];
  for (const cite of cites) {
    const label = CITE_SOURCE[cite.split(':')[0]];
    if (label && !out.includes(label)) out.push(label);
  }
  return out;
}

export function Media({ which, label, resolve }: { which: ReportMedia; label: string; resolve?: MediaResolver }): ReactElement {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;
    setUrl(null); setFailed(false);
    if (resolve) void resolve(which, controller.signal).then((blob) => {
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [resolve, which]);
  return <div className={which === 'narration' ? 'narration-player' : undefined}>
    <p>{label}</p>
    {url ? which === 'spectrogram'
      ? <img src={url} alt="Spectrogram of the engine sound clip" />
      : <audio controls preload="none" src={url} aria-label={label} data-testid={which === 'narration' ? 'narration-audio' : 'engine-audio-player'} />
      : <p className="muted">{failed || !resolve ? `${which === 'spectrogram' ? 'Spectrogram' : 'Audio'} unavailable. The written report remains available.` : 'Loading report media…'}</p>}
  </div>;
}

function Vibration({ report }: { report: PaidReport }): ReactElement {
  const vibration = report.vibration;
  const source = report.additional_checks?.find((check) => check.kind === 'rig_telemetry')?.source;
  const points = vibration?.points.filter((p) => Number.isFinite(p.t_s) && p.accel_rms !== null && Number.isFinite(p.accel_rms)) ?? [];
  if (vibration?.state !== 'assessed' || points.length < 2) return <></>;
  const maxT = Math.max(1, ...points.map((p) => p.t_s));
  const maxY = Math.max(1, ...points.map((p) => p.accel_rms!));
  // Break the line at missing readings rather than interpolate an unmeasured value.
  let connected = false;
  const path = vibration.points.map((p) => {
    if (!Number.isFinite(p.t_s) || p.accel_rms === null || !Number.isFinite(p.accel_rms)) { connected = false; return ''; }
    const segment = `${connected ? 'L' : 'M'}${40 + p.t_s / maxT * 540},${160 - p.accel_rms / maxY * 130}`;
    connected = true; return segment;
  }).join(' ');
  return <figure data-testid="vibration-chart" className="vib">
    <svg viewBox="0 0 600 200" role="img" aria-label="Vibration over time" className="vib-svg">
      <path d="M40 20V160H580" fill="none" stroke="currentColor" />
      <path d={path} fill="none" stroke="currentColor" strokeWidth="2" />
      <text x="40" y="185">0 s</text><text x="520" y="185">{maxT.toFixed(1)} s</text>
      <text x="45" y="16">Acceleration RMS (mg), uncalibrated</text>
    </svg>
    <figcaption>Vibration · {source ? provenance(source.provenance) : 'Provenance unavailable'} · Series source: {vibration.series_source}. Uncalibrated estimates, not a diagnosis.</figcaption>
  </figure>;
}


type Check = NonNullable<PaidReport['additional_checks']>[number];

/** A source counts as recorded only when it has an assessed summary; anything else goes to the end list with its reason. */
export function recordedCheck(report: PaidReport, kind: Check['kind']): Check | null {
  const check = report.additional_checks?.find((row) => row.kind === kind);
  return check?.checked && check.source?.summary?.state === 'assessed' ? check : null;
}

export function dashboardRecorded(report: PaidReport): boolean {
  return report.dashboard?.assessment?.state === 'assessed';
}

function checkReason(report: PaidReport, kind: Check['kind']): string {
  const summary = report.additional_checks?.find((row) => row.kind === kind)?.source?.summary;
  return summary && 'reason' in summary && summary.reason ? words(summary.reason) : '';
}

/** Sources that were not recorded, each with its reason in plain words, for the end list. */
export function notRecordedSources(report: PaidReport): Array<{ key: string; label: string; reason: string }> {
  const rows: Array<{ key: string; label: string; reason: string }> = [];
  if (!recordedCheck(report, 'rig_telemetry')) rows.push({ key: 'rig', label: 'Sensor pod', reason: checkReason(report, 'rig_telemetry') });
  if (!recordedCheck(report, 'obd_snapshot')) rows.push({ key: 'obd', label: 'OBD', reason: checkReason(report, 'obd_snapshot') });
  if (!recordedCheck(report, 'engine_audio')) rows.push({ key: 'audio', label: 'Engine audio', reason: checkReason(report, 'engine_audio') });
  if (!dashboardRecorded(report)) rows.push({ key: 'dash', label: 'Dashboard clip', reason: report.dashboard?.assessment?.reason ? words(report.dashboard.assessment.reason) : '' });
  return rows;
}

function SourcePill({ value }: { value: string }): ReactElement {
  return <Pill tone={isSim(value) ? 'synthetic' : 'optional'}>{provenance(value)}</Pill>;
}

/** Engine faults: OBD codes and the sensor pod verdict. Only recorded sources appear. */
export function EngineGroup({ report }: { report: PaidReport }): ReactElement | null {
  const obd = recordedCheck(report, 'obd_snapshot');
  const rig = recordedCheck(report, 'rig_telemetry');
  if (!obd && !rig) return null;
  const obdSummary = obd?.source?.summary;
  const rigSummary = rig?.source?.summary;
  return (
    <div className="group" data-testid="group-engine">
      <h3 className="group-title">Engine</h3>
      {obd && obdSummary?.kind === 'obd_snapshot' && (
        <div className="fault" data-testid="check-obd_snapshot">
          <p><SourcePill value={obd.source!.provenance} /> <span className="muted">OBD · {obd.source!.device}</span></p>
          {!(obdSummary.dtcs.length > 0 || obdSummary.mil_on || obdSummary.codes_recently_cleared === true) && <p>No trouble codes stored</p>}
          {obdSummary.mil_on && <p><b>Check-engine light is on.</b></p>}
          {obdSummary.dtcs.length > 0 && <p><b>Trouble codes:</b> {obdSummary.dtcs.map((d) => `${d.code} (${d.status})`).join(', ')}</p>}
          {obdSummary.codes_recently_cleared === true && <p><b>Codes were cleared recently.</b> That can hide a fault.</p>}
          {obdSummary.incomplete_monitors.length > 0 && <p className="muted">Incomplete monitors: {obdSummary.incomplete_monitors.map(words).join(', ')}</p>}
        </div>
      )}
      {rig && rigSummary?.kind === 'rig_telemetry' && (
        <div className="fault" data-testid="check-rig_telemetry">
          <p><SourcePill value={rig.source!.provenance} /> <span className="muted">Sensor pod · {rig.source!.device}</span></p>
          {rigSummary.model_verdict
            ? <p><b>Engine anomaly screening: {words(rigSummary.model_verdict.verdict)}.</b> Relative to a baseline; not a diagnosis.</p>
            : <p>No irregular vibration reported.</p>}
          {rigSummary.flags.length > 0 && <p className="muted">{rigSummary.flags.map(words).join(', ')}</p>}
          <p className="muted">{rigSummary.rpm_estimate != null ? `About ${rigSummary.rpm_estimate} rpm. ` : ''}{rigSummary.sample_count} samples over {rigSummary.duration_s} s.</p>
          {rigSummary.irregular_windows.length > 0 && <p className="muted">Irregular windows (unverified): {rigSummary.irregular_windows.map((w) => `${w.t_ms / 1000} s: ${w.rms_mg} mg`).join('; ')}</p>}
          <Vibration report={report} />
        </div>
      )}
      {report.mechanical_breakdown && (
        <div data-testid="mechanical-breakdown" className="fault">
          <p><b>How the Mechanical score was reached</b></p>
          <ul className="plain-list">{report.mechanical_breakdown.deductions.map((d) => <li key={d.code}>{d.label}: {d.points} points deducted</li>)}</ul>
          {report.mechanical_breakdown.flags.map((f) => <p key={f} className="muted">{words(f)}</p>)}
        </div>
      )}
    </div>
  );
}

/** Sound: the engine-audio verdict with the player and spectrogram. */
export function SoundGroup({ report, resolveMedia }: { report: PaidReport; resolveMedia?: MediaResolver }): ReactElement | null {
  const audio = recordedCheck(report, 'engine_audio');
  const summary = audio?.source?.summary;
  if (!audio || summary?.kind !== 'engine_audio') return null;
  return (
    <div className="group" data-testid="group-sound">
      <h3 className="group-title">Sound</h3>
      <div className="fault" data-testid="check-engine_audio">
        <p><SourcePill value={audio.source!.provenance} /> <span className="muted">Engine audio · {audio.source!.device}</span></p>
        <p>{summary.flags.length ? <b>{summary.flags.map(words).join(', ')}</b> : 'Nothing flagged in the engine sound.'}</p>
        <p className="muted">Clip length {summary.duration_s ?? 'not available'} s. Average loudness {summary.rms_db ?? 'not available'} dBFS; peak {summary.peak_db ?? 'not available'} dBFS.</p>
        {audio.source?.provenance === 'synthetic' && <p className="muted">Synthetic bench or sample clip; not a recording of this vehicle.</p>}
        <Media which="audio" label="Engine sound clip" resolve={resolveMedia} />
        {summary.spectrogram_sha256 && <Media which="spectrogram" label="Engine audio spectrogram" resolve={resolveMedia} />}
      </div>
    </div>
  );
}

/** Dashboard: warning lights read from the dashboard clip. */
export function DashboardGroup({ report }: { report: PaidReport }): ReactElement | null {
  const dash = report.dashboard;
  const a = dash?.assessment;
  if (!dash || !a || a.state !== 'assessed') return null;
  return (
    <div className="group" data-testid="group-dashboard">
      <h3 className="group-title">Dashboard</h3>
      <div className="fault" data-testid="dashboard-check">
        <p><SourcePill value={a.provenance} /></p>
        {a.lit.length === 0 && a.flags.length === 0 && <p>No warning lights read from the dashboard clip.</p>}
        {a.lit.map((light, i) => <p key={i}><b>{words(light.indicator)}</b> ({light.severity}; uncalibrated model confidence {light.confidence}/5)</p>)}
        {a.flags.map((flag) => <p key={flag}>{words(flag)}</p>)}
        <p className="muted">{dash.note}</p>
      </div>
    </div>
  );
}

/** The narration player, mounted only once the reader asks to listen, so no audio is fetched before then. */
export function NarrationPlayer({ report, resolveMedia }: { report: PaidReport; resolveMedia?: MediaResolver }): ReactElement | null {
  const narration = report.narration_audio;
  if (!narration) return null;
  return (
    <section className="narration" id="r-advice" aria-label="Spoken report">
      <p><Pill tone={isSim(narration.provenance) ? 'synthetic' : 'optional'}>{provenance(narration.provenance)}</Pill></p>
      <Media which="narration" label={narration.label} resolve={resolveMedia} />
    </section>
  );
}

/** The AI advisory, only when available. An unavailable advisory is one line in the end list instead. */
export function ReportAdvice({ report }: { report: PaidReport }): ReactElement | null {
  const advisory = report.advisory;
  if (!advisory || advisory.status !== 'available') return null;
  const list = (items: typeof advisory.questions_for_seller): ReactElement => (
    <ul className="plain-list">{items.map((q, i) => {
      const labels = citeLabels(q.cites);
      return <li key={i}>{q.text}{labels.length > 0 && <span className="muted"> ({labels.join(', ')})</span>}</li>;
    })}</ul>
  );
  return (
    <section className="rsec" data-testid="advisory" aria-labelledby="advisory-h">
      <h2 id="advisory-h">Advice</h2>
      <p><strong>AI advisory · not a finding</strong> <Pill tone={isSim(advisory.provenance) ? 'synthetic' : 'optional'}>{provenance(advisory.provenance)}</Pill></p>
      {advisory.summary && <p data-testid="advisory-summary">{advisory.summary}</p>}
      {advisory.questions_for_seller.length > 0 && <><h3 className="group-title">Ask the seller</h3>{list(advisory.questions_for_seller)}</>}
      {advisory.mechanic_checks.length > 0 && <><h3 className="group-title">Ask a mechanic</h3>{list(advisory.mechanic_checks)}</>}
      <p className="muted small">Generated advice. It does not affect any score.</p>
    </section>
  );
}
