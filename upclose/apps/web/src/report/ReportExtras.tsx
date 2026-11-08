import { useEffect, useState, type ReactElement } from 'react';
import type { PaidReport, ReportMedia } from '../api/client';

export type MediaResolver = (which: ReportMedia, signal: AbortSignal) => Promise<Blob>;
const words = (text: string): string => text.replaceAll('_', ' ');
const provenance = (value: string): string => value === 'synthetic' || value === 'fixture' ? 'Synthetic sample data' : value === 'cached' ? 'Cached recording' : 'Live';
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
      ? <img src={url} alt="Spectrogram of the engine sound clip" style={{ maxWidth: '100%' }} />
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
  return <figure data-testid="vibration-chart">
    <svg viewBox="0 0 600 200" role="img" aria-label="Vibration over time" style={{ width: '100%' }}>
      <path d="M40 20V160H580" fill="none" stroke="currentColor" />
      <path d={path} fill="none" stroke="currentColor" strokeWidth="2" />
      <text x="40" y="185">0 s</text><text x="520" y="185">{maxT.toFixed(1)} s</text>
      <text x="45" y="16">Acceleration RMS (mg), uncalibrated</text>
    </svg>
    <figcaption>Vibration · {source ? provenance(source.provenance) : 'Provenance unavailable'} · Series source: {vibration.series_source}. Uncalibrated estimates, not a diagnosis.</figcaption>
  </figure>;
}

export function ReportExtras({ report, resolveMedia }: { report: PaidReport; resolveMedia?: MediaResolver }): ReactElement {
  const advisory = report.advisory;
  return <>
    <section className="rsec" id="r-mechanical" aria-labelledby="mechanical-h">
      <h2 id="mechanical-h">Mechanical checks</h2>
      <p className="sub">These checks feed the Mechanical score using unvalidated weights. Missing evidence is not a pass. Values are uncalibrated estimates, not measurements.</p>
      {report.dashboard && <article className="miss" data-testid="dashboard-check">
        <h3>Dashboard</h3>
        <p><strong>{report.dashboard.assessment?.state === 'assessed' ? 'Checked' : 'Not checked'}</strong></p>
        {report.dashboard.assessment && <>
          <p>{provenance(report.dashboard.assessment.provenance)}</p>
          {report.dashboard.assessment.reason && <p>{words(report.dashboard.assessment.reason)}</p>}
          {report.dashboard.assessment.lit.map((light, i) => <p key={i}>{words(light.indicator)} ({light.severity}; uncalibrated model confidence {light.confidence}/5)</p>)}
          {report.dashboard.assessment.flags.map((flag) => <p key={flag}>{words(flag)}</p>)}
        </>}
        <p className="muted">{report.dashboard.note}</p>
      </article>}
      {Object.entries(LABELS).map(([kind, label]) => {
        const check = report.additional_checks?.find((row) => row.kind === kind);
        const source = check?.source;
        const summary = source?.summary;
        const assessed = check?.checked && summary?.state === 'assessed';
        return <article className="miss" key={kind} data-testid={`check-${kind}`}>
          <h3>{label}</h3>
          <p><strong>{assessed ? 'Checked' : 'Not checked'}</strong>{source && ` · ${provenance(source.provenance)} · ${source.device}`}</p>
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
        </article>;
      })}
      {report.mechanical_breakdown && <div data-testid="mechanical-breakdown">
        <h3>How the Mechanical score was reached</h3>
        <ul>{report.mechanical_breakdown.deductions.map((d) => <li key={d.code}>{d.label}: {d.points} points deducted</li>)}</ul>
        {report.mechanical_breakdown.flags.map((f) => <p key={f}>{words(f)}</p>)}
      </div>}
      <Vibration report={report} />
    </section>
    {report.narration_audio && <section className="rsec" aria-label="Spoken report">
      <h2>Listen to your report</h2>
      <p>{provenance(report.narration_audio.provenance)}</p>
      <Media which="narration" label={report.narration_audio.label} resolve={resolveMedia} />
    </section>}
    {advisory && <section className="rsec" data-testid="advisory" aria-labelledby="advisory-h">
      <h2 id="advisory-h">Advisory</h2>
      <p><strong>{advisory.label}</strong> · {provenance(advisory.provenance)}</p>
      {advisory.status === 'available' ? <>
        <h3>Questions for the seller</h3>
        <ul>{advisory.questions_for_seller.map((q, i) => <li key={i}>{q.text} <span className="muted">({q.cites.join(', ')})</span></li>)}</ul>
        <h3>Checks for a mechanic</h3>
        <ul>{advisory.mechanic_checks.map((q, i) => <li key={i}>{q.text} <span className="muted">({q.cites.join(', ')})</span></li>)}</ul>
        <p>{advisory.summary}</p>
      </> : <p>Advisory unavailable. {advisory.reason && words(advisory.reason)}</p>}
      <p className="muted">This is generated advice, not a finding, and it does not affect any score.</p>
    </section>}
  </>;
}
