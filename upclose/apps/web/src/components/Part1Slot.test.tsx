// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { syntheticVerdict } from '../testing/fakeBackend';
import { Part1Slot } from './FreeVerdictCard';

afterEach(cleanup);

describe('Part1Slot toggle', () => {
  it('labels the toggle in words on the summary row and follows the open state', () => {
    render(<Part1Slot part1={undefined} verdict={syntheticVerdict()} />);
    const slot = screen.getByTestId('free-verdict-slot');
    const summary = slot.querySelector('summary') as HTMLElement;
    expect(within(summary).getByText('Show details')).toBeTruthy();
    expect(summary.textContent).toContain('Part 1 free result');
    expect(within(summary).getByTestId('verdict-headline').textContent).toContain('Provisional index 94.0');
    const details = slot.querySelector('details') as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event('toggle'));
    expect(within(summary).getByText('Hide details')).toBeTruthy();
    expect(within(summary).queryByText('Show details')).toBeNull();
  });

  it('keeps the verdict line inside the slot once opened', () => {
    render(<Part1Slot part1={undefined} verdict={syntheticVerdict()} />);
    expect(within(screen.getByTestId('free-verdict-slot')).getByTestId('verdict-line')).toBeTruthy();
  });
});
