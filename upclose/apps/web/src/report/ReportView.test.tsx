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
    expect(screen.getByRole('heading', { name: 'What needs attention' })).toBeTruthy();
    expect(screen.getByText('94.9')).toBeTruthy();
    expect(screen.getByText(/In the index: driver side, passenger side, front & rear with glass \(3 of 6\)./)).toBeTruthy();
    expect(screen.getByText(/Partly checked: tires./)).toBeTruthy();
    expect(screen.getByText(/It measures visible damage, not whether the car is a good buy./)).toBeTruthy();
    expect(screen.queryByText(/Based on 3 of 6/)).toBeNull();
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
    expect(screen.getByRole('heading', { level: 2, name: 'All zones' })).toBeTruthy();
    const zones = document.getElementById('r-zones')!;
    const tires = within(zones).getByRole('rowheader', { name: 'Tires' }).closest('tr')!;
    expect(tires.textContent).toMatch(/Partly checked, so no score/);
    expect(tires.textContent).not.toMatch(/\d+\.\d/);
  });
});
