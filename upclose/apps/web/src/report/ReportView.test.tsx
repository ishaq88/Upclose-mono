// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { SAMPLE_REPORT, sampleFrameUrl } from '../sample/sample';
import { ReportView } from './ReportView';

vi.mock('./TopDownMap', () => ({ default: () => <div data-testid="map" /> }));

beforeAll(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} });
});
afterEach(cleanup);

describe('ReportView', () => {
  it('leads with the verdict, then the index and lowest zone', () => {
    render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    expect(screen.getByRole('heading', { name: '1 serious issue' })).toBeTruthy();
    expect(screen.getByText('94.9')).toBeTruthy();
    expect(screen.getByText(/Based on 3 of 6 zones fully checked/)).toBeTruthy();
  });

  it('numbers findings serious-first and gives every frame an accessible enlarge control', () => {
    render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    const found = screen.getByRole('region', { name: 'What we found' });
    const titles = within(found).getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
    expect(titles[0]).toMatch(/^1 · Rust/);
    expect(within(found).getAllByRole('button', { name: /^Enlarge frame at \d+:\d\d/ }).length).toBe(SAMPLE_REPORT.findings.length + 1);
  });

  it('shows partly checked and unchecked zones without a score', () => {
    render(<ReportView report={SAMPLE_REPORT} frameUrl={sampleFrameUrl} />);
    const zones = screen.getByRole('region', { name: 'All zones' });
    const tires = within(zones).getByRole('rowheader', { name: 'Tires' }).closest('tr')!;
    expect(tires.textContent).toMatch(/Partly checked, so no score/);
    expect(tires.textContent).not.toMatch(/\d+\.\d/);
  });
});
