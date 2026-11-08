// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { CategoryScores as Categories } from '../api/client';
import { CategoryScores } from './CategoryScores';

afterEach(cleanup);
const cell = (id: string) => within(screen.getByTestId(`category-${id}`));
const scored = (score: number) => ({ state: 'assessed' as const, score, reason: null });
const none = { state: 'not_checked' as const, score: null, reason: 'No kit attached.' };
const base = { config_hash: 'h', label: 'Provisional index, not a diagnosis' };
const make = (visual: Categories['visual'], mechanical: Categories['mechanical'], overall: Categories['overall']): Categories => ({ ...base, visual, mechanical, overall });

describe('category scores', () => {
  it('shows Visual, Mechanical and Overall scores', () => {
    render(<CategoryScores categories={make(scored(88.4), scored(72), { state: 'assessed', score: 80, basis: 'blend', note: '' })} />);
    expect(cell('visual').getByText('88.4')).toBeTruthy();
    expect(cell('mechanical').getByText('72.0')).toBeTruthy();
    expect(cell('overall').getByText('80.0')).toBeTruthy();
    expect(screen.queryByText('Visual only')).toBeNull();
    expect(screen.queryByText('Not checked')).toBeNull();
  });

  it('truncates to one decimal and never rounds up', () => {
    render(<CategoryScores categories={make(scored(94.96), scored(77.99), { state: 'assessed', score: 64, basis: 'blend', note: '' })} />);
    expect(cell('visual').getByText('94.9')).toBeTruthy();
    expect(cell('mechanical').getByText('77.9')).toBeTruthy();
    expect(cell('overall').getByText('64.0')).toBeTruthy();
  });

  it('shows Visual only, with Mechanical Not checked and no overall number', () => {
    render(<CategoryScores categories={make(scored(91), none, { state: 'not_checked', score: null, basis: 'visual_only', note: 'Visual only: mechanical not checked.' })} />);
    expect(cell('mechanical').getByText('Not checked')).toBeTruthy();
    expect(cell('overall').getByText('Visual only')).toBeTruthy();
    expect(screen.getByText('Visual only: mechanical not checked.')).toBeTruthy();
    expect(cell('overall').queryByText('91.0')).toBeNull();
  });

  it('never turns a missing score into a number', () => {
    render(<CategoryScores categories={make(none, none, { state: 'not_checked', score: null, basis: 'none', note: '' })} />);
    for (const id of ['visual', 'mechanical', 'overall']) expect(cell(id).getByText('Not checked')).toBeTruthy();
    expect(screen.queryByText('Visual only')).toBeNull();
    expect(screen.getByTestId('category-scores').textContent).not.toMatch(/\d/);
  });

  it('does not show a score for a category marked not checked even if a number is present', () => {
    render(<CategoryScores categories={make({ state: 'not_checked', score: 55, reason: null }, scored(60), { state: 'not_checked', score: null, basis: 'mechanical_only', note: '' })} />);
    expect(cell('visual').getByText('Not checked')).toBeTruthy();
    expect(screen.queryByText('Visual only')).toBeNull();
  });
});
