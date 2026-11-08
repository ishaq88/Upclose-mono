import { useEffect, useState, type ReactElement } from 'react';
import type { PaidReport, ReportMedia } from '../api/client';
import { Card, Disclosure, Pill } from '../ui';

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
const LABELS = { rig_telemetry: 'Sensor pod', obd_snapshot: 'OBD snapshot', engine_audio: 'Engine audio' };

function Media({ which, label, resolve }: { which: ReportMedia; label: string; resolve?: MediaResolver }): ReactElement {
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
  if (vibration?.state !== 'assessed' || points.length < 2) return <p>Vibration chart: Not checked. {words(vibration?.reason ?? 'No usable vibration series was attached.')} This is not a pass.</p>;
  const maxT = Math.max(1, ...points.map((p) => p.t_s));
  const maxY = Math.max(1, ...points.map((p) => p.accel_rms!));
  // Break the line at missing readings rather than interpolate an unmeasured value.
  let connected = false;
  const path = vibration.points.map((p) => {
    if (!Number.isFinite(p.t_s) || p.accel_rms === null || !Number.isFinite(p.accel_rms)) { connected = false; return ''; }
    const segment = `${connected ? 'L' : 'M'}${40 + p.t_s / maxT * 540},${160 - p.accel_rms / maxY * 130}`;
    connected = true; return segment;
  }).join(' ');
  return <Card as="figure" padding="md" data-testid="vibration-chart" className="check-card">
    <svg viewBox="0 0 600 200" role="img" aria-label="Vibration over time" className="vib-svg">
      <path d="M40 20V160H580" fill="none" stroke="currentColor" />
      <path d={path} fill="none" stroke="currentColor" strokeWidth="2" />
      <text x="40" y="185">0 s</text><text x="520" y="185">{maxT.toFixed(1)} s</text>
      <text x="45" y="16">Acceleration RMS (mg), uncalibrated</text>
    </svg>
    <figcaption>Vibration · {source ? provenance(source.provenance) : 'Provenance unavailable'} · Series source: {vibration.series_source}. Uncalibrated estimates, not a diagnosis.</figcaption>
  </Card>;
}

/** One plain line for the closed Mechanical checks disclosure. */
export function mechanicalSummary(report: PaidReport): string {
  const kinds = Object.keys(LABELS);
  const done = kinds.filter((kind) => { const c = report.additional_checks?.find((row) => row.kind === kind); return c?.checked && c.source?.summary?.state === 'assessed'; }).length;
  return done === 0 ? 'Not checked' : `${done} of ${kinds.length} checked`;
}

export function ReportMechanical({ report, resolveMedia, defaultOpen = false }: { report: PaidReport; resolveMedia?: MediaResolver; defaultOpen?: boolean }): ReactElement {
  return <>
    <Disclosure id="r-mechanical" title="Mechanical checks" headingLevel={2} summary={mechanicalSummary(report)} defaultOpen={defaultOpen}>
      <p className="muted">These feed the Mechanical score. Missing evidence is not a pass, and values are uncalibrated estimates.</p>
      {report.dashboard && <Card as="article" padding="md" className="check-card" data-testid="dashboard-check">
        <div className="check-head"><h3>Dashboard</h3><Pill tone={report.dashboard.assessment?.state === 'assessed' ? 'ok' : 'optional'}>{report.dashboard.assessment?.state === 'assessed' ? 'Checked' : 'Not checked'}</Pill></div>
        {report.dashboard.assessment && <>
          <p><Pill tone={isSim(report.dashboard.assessment.provenance) ? 'synthetic' : 'optional'}>{provenance(report.dashboard.assessment.provenance)}</Pill></p>
          {report.dashboard.assessment.reason && <p>{words(report.dashboard.assessment.reason)}</p>}
          {report.dashboard.assessment.lit.map((light, i) => <p key={i}>{words(light.indicator)} ({light.severity}; uncalibrated model confidence {light.confidence}/5)</p>)}
          {report.dashboard.assessment.flags.map((flag) => <p key={flag}>{words(flag)}</p>)}
        </>}
        <p className="muted">{report.dashboard.note}</p>
      </Card>}
      {Object.entries(LABELS).map(([kind, label]) => {
        const check = report.additional_checks?.find((row) => row.kind === kind);
        const source = check?.source;
        const summary = source?.summary;
        const assessed = check?.checked && summary?.state === 'assessed';
        return <Card as="article" padding="md" className="check-card" key={kind} data-testid={`check-${kind}`}>
          <div className="check-head"><h3>{label}</h3><Pill tone={assessed ? 'ok' : 'optional'}>{assessed ? 'Checked' : 'Not checked'}</Pill></div>
          {source && <p><Pill tone={isSim(source.provenance) ? 'synthetic' : 'optional'}>{provenance(source.provenance)}</Pill> <span className="muted">{source.device}</span></p>}
          {!assessed && <p>{words(summary?.reason ?? 'No assessed data was attached.')} This is not a pass.</p>}
          {assessed && summary && <>
            <p>Flags: {summary.flags.length ? summary.flags.map(words).join(', ') : 'None reported'}</p>
            {summary.kind === 'rig_telemetry' && <>
              <p>{summary.sample_count} samples over {summary.duration_s} s. Estimated engine speed: {summary.rpm_estimate ?? 'Not available'} rpm. Dominant frequency: {summary.dominant_hz ?? 'Not available'} Hz.</p>
              {summary.model_verdict && <p>Engine anomaly screening: {words(summary.model_verdict.verdict)}. Relative to a baseline; not a diagnosis.</p>}
              {summary.irregular_windows.length > 0 && <p>Irregular vibration windows (unverified): {summary.irregular_windows.map((w) => `${w.t_ms / 1000} s: ${w.rms_mg} mg`).join('; ')}</p>}
            </>}
            {summary.kind === 'obd_snapshot' && <>
              <p>Check-engine light: {summary.mil_on ? 'On' : 'Off'}. Trouble codes: {summary.dtcs.map((d) => `${d.code} (${d.status})`).join(', ') || 'None reported'}.</p>
              <p>Incomplete monitors: {summary.incomplete_monitors.map(words).join(', ') || 'None reported'}. Codes recently cleared: {summary.codes_recently_cleared === null ? 'Not available' : summary.codes_recently_cleared ? 'Yes' : 'No'}.</p>
            </>}
            {summary.kind === 'engine_audio' && <>
              <p>Clip length: {summary.duration_s ?? 'Not available'} s. Average loudness: {summary.rms_db ?? 'Not available'} dBFS; peak: {summary.peak_db ?? 'Not available'} dBFS.</p>
              {source?.provenance === 'synthetic' && <p>Synthetic bench or sample clip; not a recording of this vehicle.</p>}
              <Media which="audio" label="Engine sound clip" resolve={resolveMedia} />
              {summary.spectrogram_sha256 ? <Media which="spectrogram" label="Engine audio spectrogram" resolve={resolveMedia} /> : <p>Spectrogram unavailable.</p>}
            </>}
          </>}
        </Card>;
      })}
      {report.mechanical_breakdown && <Card padding="md" data-testid="mechanical-breakdown" className="check-card">
        <h3>How the Mechanical score was reached</h3>
        <ul>{report.mechanical_breakdown.deductions.map((d) => <li key={d.code}>{d.label}: {d.points} points deducted</li>)}</ul>
        {report.mechanical_breakdown.flags.map((f) => <p key={f}>{words(f)}</p>)}
      </Card>}
      <Vibration report={report} />
    </Disclosure>
  </>;
}

/** Narration player and generated advice: the last thing a reader needs, so it sits after the evidence. */
export function ReportAdvice({ report, resolveMedia }: { report: PaidReport; resolveMedia?: MediaResolver }): ReactElement | null {
  const advisory = report.advisory;
  if (!report.narration_audio && !advisory) return null;
  return <div id="r-advice" className="advice-group">
    {report.narration_audio && <section className="rsec" aria-label="Spoken report">
      <h2>Listen to your report</h2>
      <p><Pill tone={isSim(report.narration_audio.provenance) ? 'synthetic' : 'optional'}>{provenance(report.narration_audio.provenance)}</Pill></p>
      <Media which="narration" label={report.narration_audio.label} resolve={resolveMedia} />
    </section>}
    {advisory && <section className="rsec" data-testid="advisory" aria-labelledby="advisory-h">
      <h2 id="advisory-h">Advisory</h2>
      <p><strong>{advisory.label}</strong> <Pill tone={isSim(advisory.provenance) ? 'synthetic' : 'optional'}>{provenance(advisory.provenance)}</Pill></p>
      {advisory.status === 'available'
        ? <>{advisory.summary && <p data-testid="advisory-summary">{advisory.summary}</p>}<p className="muted">The questions and checks it suggests are in your checklist below.</p></>
        : <p>Advisory unavailable. {advisory.reason && words(advisory.reason)}</p>}
      <p className="muted">This is generated advice, not a finding, and it does not affect any score.</p>
    </section>}
  </div>;
}

/** A checklist row: what to do or ask, and where it came from in plain words. */
export interface ChecklistItem { text: string; source: string; findingIds: readonly string[] }

/** Narration items plus the advisory's questions and mechanic checks, merged. Every item names its source; unknown sources are never shown raw. */
export function checklistItems(report: PaidReport): ChecklistItem[] {
  const items: ChecklistItem[] = report.narration.map((row) => ({ text: row.text, source: row.finding_ids.length ? 'From your findings' : 'From the report', findingIds: row.finding_ids }));
  const advisory = report.advisory;
  if (advisory && advisory.status === 'available') {
    for (const q of [...advisory.questions_for_seller, ...advisory.mechanic_checks]) {
      const labels = citeLabels(q.cites);
      items.push({ text: q.text, source: labels.length ? labels.join(', ') : 'Suggested by the advisory', findingIds: [] });
    }
  }
  return items;
}
