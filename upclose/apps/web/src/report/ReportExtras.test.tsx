// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { citeLabels, ReportAdvice } from './ReportExtras';
import { SAMPLE_REPORT } from '../sample/sample';

afterEach(cleanup);

describe('advisory citations', () => {
  it('maps source ids to plain words and omits unknown ones', () => {
    expect(citeLabels(['rig_telemetry:rough_vibration', 'obd_snapshot:mil_on', 'obd_snapshot:cold_start', 'mystery_source:x']))
      .toEqual(['From the sensor pod', 'From the OBD snapshot']);
    expect(citeLabels(['engine_audio', 'dashboard', 'video'])).toEqual(['From the engine audio', 'From the dashboard', 'From the video']);
    expect(citeLabels(['unknown'])).toEqual([]);
  });

  const advisory = (summary: string) => ({
    label: 'Advisory', provenance: 'fixture', status: 'available', summary, reason: null,
    questions_for_seller: [{ text: 'Ask about maintenance.', cites: ['rig_telemetry:rpm_estimate', 'nope'] }],
    mechanic_checks: [{ text: 'Have a mechanic check the idle.', cites: ['obd_snapshot:mil_on'] }],
  });
  const view = (summary: string) => render(<ReportAdvice report={{ ...SAMPLE_REPORT, narration_audio: null, advisory: advisory(summary) } as never} />);

  it('never prints a raw id', () => {
    view('x');
    const text = screen.getByTestId('advisory').textContent!;
    expect(text).toContain('(From the sensor pod)');
    expect(text).toContain('(From the OBD snapshot)');
    expect(text).not.toMatch(/rig_telemetry|obd_snapshot|nope/);
  });

  it('renders the summary once, as the lead paragraph before the first list heading', () => {
    const text = 'The idle is rough; have a mechanic check the idle.';
    view(text);
    const html = screen.getByTestId('advisory').textContent!;
    expect(html.split(text)).toHaveLength(2);
    expect(html.indexOf(text)).toBeLessThan(html.indexOf('Questions for the seller'));
  });

  it('renders nothing for an empty summary', () => {
    view('');
    expect(screen.queryByTestId('advisory-summary')).toBeNull();
  });
});
