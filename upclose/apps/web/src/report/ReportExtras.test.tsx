// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { checklistItems, citeLabels, ReportAdvice } from './ReportExtras';
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

  it('merges narration and advisory items into one checklist, each naming its source in plain words', () => {
    const report = { ...SAMPLE_REPORT, narration_audio: null, advisory: advisory('x') } as never;
    const items = checklistItems(report);
    const ask = items.find((i) => i.text === 'Ask about maintenance.')!;
    const check = items.find((i) => i.text === 'Have a mechanic check the idle.')!;
    expect(ask.source).toBe('From the sensor pod');
    expect(check.source).toBe('From the OBD snapshot');
    expect(items.length).toBe(SAMPLE_REPORT.narration.length + 2);
    for (const item of items) expect(item.source).not.toMatch(/rig_telemetry|obd_snapshot|nope|_/);
  });

  it('names a plain fallback when an advisory item has no known source', () => {
    const odd = { ...advisory('x'), questions_for_seller: [{ text: 'Odd one.', cites: ['mystery_source:x'] }], mechanic_checks: [] };
    const items = checklistItems({ ...SAMPLE_REPORT, advisory: odd } as never);
    expect(items.find((i) => i.text === 'Odd one.')!.source).toBe('Suggested by the advisory');
  });

  it('leaves the advisory questions out of the Advisory block, and renders the summary once', () => {
    const text = 'The idle is rough; have a mechanic check the idle.';
    view(text);
    const block = screen.getByTestId('advisory').textContent!;
    expect(block.split(text)).toHaveLength(2);
    expect(block).not.toContain('Ask about maintenance.');
    expect(screen.getByTestId('advisory-summary').textContent).toBe(text);
  });

  it('renders nothing for an empty summary', () => {
    view('');
    expect(screen.queryByTestId('advisory-summary')).toBeNull();
  });
});
