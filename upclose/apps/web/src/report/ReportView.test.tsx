// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SAMPLE_REPORT, sampleFrameUrl } from '../sample/sample';
import { ReportView } from './ReportView';

afterEach(cleanup);

describe('ReportView (one column)', () => {
  it('leads with the verdict and a red icon for serious, never a green check, and one index line', () => {
    const { container } = render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    expect(screen.getByRole('heading', { name: '1 serious issue' })).toBeTruthy();
    expect(screen.getByText(/Condition index:/).textContent).toContain('94.9');
    expect(container.querySelector('.k-ok')).toBeNull();
    expect(container.querySelector('.rv-side, .toc, .map-col, .rv-chips')).toBeNull();
  });

  it('shows a neutral, not green, icon when nothing is serious', () => {
    const report = { ...SAMPLE_REPORT, findings: SAMPLE_REPORT.findings.map((f) => ({ ...f, flags: [] })) };
    const { container } = render(<ReportView report={report} frameUrl={sampleFrameUrl} />);
    expect(container.querySelector('.k-neutral')).toBeTruthy();
    expect(container.querySelector('.k-ok')).toBeNull();
  });

  it('numbers findings serious-first and gives every frame an accessible enlarge control', () => {
    render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    const found = screen.getByRole('region', { name: 'What we found' });
    const titles = within(found).getAllByRole('heading', { level: 4 }).map((h) => h.textContent);
    expect(titles[0]).toMatch(/^1 · Rust/);
    expect(within(found).getAllByRole('button', { name: /^Enlarge frame at \d+:\d\d/ }).length).toBe(SAMPLE_REPORT.findings.length + 1);
  });

  it('shows only recorded sources in the faults, and lists what was not recorded at the end', () => {
    render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    expect(screen.queryByTestId('group-engine')).toBeNull();
    expect(screen.queryByTestId('group-sound')).toBeNull();
    expect(screen.queryByTestId('group-dashboard')).toBeNull();
    expect(screen.queryByTestId('advisory')).toBeNull();
    const list = screen.getByRole('region', { name: "Coverage and what wasn't recorded" });
    expect(list.textContent).toContain('Sensor pod');
    expect(list.textContent).toContain('OBD');
    expect(list.textContent).toContain('AI advice');
    const tires = within(list).getByText('Tires').closest('li')!;
    expect(tires.textContent).toMatch(/Partly checked/);
    expect(tires.textContent).not.toMatch(/\d+\.\d/);
  });

  it('removes the old blocks: checklist, all-zones table, tiles and chips', () => {
    render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    for (const name of ['Your checklist', 'All zones', "What we couldn't check", 'What needs attention']) expect(screen.queryByRole('heading', { name })).toBeNull();
    expect(screen.queryByRole('navigation', { name: 'Report sections' })).toBeNull();
  });
});
