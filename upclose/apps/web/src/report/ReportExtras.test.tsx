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
    expect(citeLabels(['finding:2', 'finding:2', 'finding:10', 'coverage:underbody', 'coverage:tires', 'obd_snapshot:mil_on']))
      .toEqual(['From finding 2', 'From finding 10', 'From what wasn’t checked', 'From the OBD snapshot']);
    expect(citeLabels(['finding', 'finding:x', 'finding:0', 'finding:<b>'])).toEqual([]);
  });

  it('shows a finding or coverage citation as plain words, never the raw key', () => {
    const report = { ...SAMPLE_REPORT, advisory: {
      label: 'Advisory', provenance: 'live', status: 'available', summary: 'Ask first.', reason: null,
      questions_for_seller: [{ text: 'Ask about the rust.', cites: ['finding:1'] }],
      mechanic_checks: [{ text: 'Look under the car.', cites: ['coverage:underbody'] }] } } as never;
    render(<ReportAdvice report={report} />);
    const text = screen.getByTestId('advisory').textContent ?? '';
    expect(text).toContain('(From finding 1)');
    expect(text).toContain('(From what wasn’t checked)');
    expect(text).not.toMatch(/finding:|coverage:/);
  });

  const advisory = (summary: string) => ({
    label: 'Advisory', provenance: 'fixture', status: 'available', summary, reason: null,
    questions_for_seller: [{ text: 'Ask about maintenance.', cites: ['rig_telemetry:rpm_estimate', 'nope'] }],
    mechanic_checks: [{ text: 'Have a mechanic check the idle.', cites: ['obd_snapshot:mil_on'] }],
  });
  const view = (summary: string) => render(<ReportAdvice report={{ ...SAMPLE_REPORT, advisory: advisory(summary) } as never} />);

  it('shows the advice with plain-word sources and never a raw id', () => {
    view('The idle is rough; have a mechanic check the idle.');
    const block = screen.getByTestId('advisory');
    expect(screen.getByRole('heading', { level: 2, name: 'Advice' })).toBeTruthy();
    expect(block.textContent).toContain('AI advisory · not a finding');
    expect(block.textContent).toContain('Ask the seller');
    expect(block.textContent).toContain('Ask a mechanic');
    expect(block.textContent).toContain('(From the sensor pod)');
    expect(block.textContent).toContain('(From the OBD snapshot)');
    expect(block.textContent).not.toMatch(/rig_telemetry|obd_snapshot|nope/);
    expect(screen.getByTestId('advisory-summary').textContent).toContain('The idle is rough');
  });

  it('renders nothing when the advisory is unavailable (it becomes a line in the end list)', () => {
    const report = { ...SAMPLE_REPORT, advisory: { ...advisory('x'), status: 'not_available' } } as never;
    const { container } = render(<ReportAdvice report={report} />);
    expect(container.textContent).toBe('');
  });
});
