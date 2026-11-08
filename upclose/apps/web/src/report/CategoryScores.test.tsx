// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { CategoryScores } from './CategoryScores';

afterEach(cleanup);
const cell = (id: string) => within(screen.getByTestId(`category-${id}`));

describe('category scores', () => {
  it('shows Visual, Mechanical and Overall scores', () => {
    render(<CategoryScores scores={{ visual: 88.4, mechanical: 72, overall: 80 }} />);
    expect(cell('visual').getByText('88')).toBeTruthy();
    expect(cell('mechanical').getByText('72')).toBeTruthy();
    expect(cell('overall').getByText('80')).toBeTruthy();
    expect(screen.queryByText('Visual only')).toBeNull();
    expect(screen.queryByText('Not checked')).toBeNull();
  });

  it('shows Visual only, with Mechanical Not checked, when nothing mechanical was captured', () => {
    render(<CategoryScores scores={{ visual: 91, mechanical: null, overall: 91 }} />);
    expect(cell('mechanical').getByText('Not checked')).toBeTruthy();
    expect(cell('overall').getByText('Visual only')).toBeTruthy();
    expect(screen.getByText(/overall score reflects the visual inspection only/)).toBeTruthy();
  });

  it('never turns a missing score into a number', () => {
    render(<CategoryScores scores={{ visual: null, mechanical: null, overall: null }} />);
    for (const id of ['visual', 'mechanical', 'overall']) expect(cell(id).getByText('Not checked')).toBeTruthy();
    expect(screen.queryByText('Visual only')).toBeNull();
    expect(screen.getByTestId('category-scores').textContent).not.toMatch(/\d/);
  });

  it('shows Not checked for Visual when only a mechanical score exists', () => {
    render(<CategoryScores scores={{ visual: null, mechanical: 60, overall: 60 }} />);
    expect(cell('visual').getByText('Not checked')).toBeTruthy();
    expect(screen.queryByText('Visual only')).toBeNull();
  });
});
