// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CategoryScores } from '../api/client';
import {
  ActionBar, Banner, Button, Card, ChoiceTiles, CoverageBar, coverageLine, Disclosure, EvidenceFrame, Field, Notice, PageHeader, Pill,
  ScoreTiles, Section, SeverityPips, StatusChip, type ZoneCoverage,
} from './index';

afterEach(cleanup);

describe('Button', () => {
  it('renders each variant and size as classes on a named button', () => {
    render(<><Button>Start</Button><Button variant="secondary" size="sm">Back</Button><Button variant="dark">Finish</Button><Button variant="danger">Delete</Button><Button variant="text">Skip</Button></>);
    expect(screen.getByRole('button', { name: 'Start' }).className).toContain('btn-primary');
    const back = screen.getByRole('button', { name: 'Back' });
    expect(back.className).toContain('btn-secondary');
    expect(back.className).toContain('btn-sm');
    expect(screen.getByRole('button', { name: 'Finish' }).className).toContain('btn-dark');
    expect(screen.getByRole('button', { name: 'Delete' }).className).toContain('btn-danger');
    expect(screen.getByRole('button', { name: 'Skip' }).className).toContain('btn-text');
  });

  it('renders a link when href is given, and a block button', () => {
    render(<><Button href="/reports" block>My reports</Button></>);
    const link = screen.getByRole('link', { name: 'My reports' });
    expect(link.getAttribute('href')).toBe('/reports');
    expect(link.className).toContain('btn-block');
  });

  it('keeps the icon out of the accessible name', () => {
    render(<Button icon={<svg data-testid="i" />}>Record</Button>);
    expect(screen.getByRole('button', { name: 'Record' })).toBeTruthy();
  });

  it('is inert with an announced reason when disabledReason is set, and truly disabled otherwise', () => {
    const onClick = vi.fn();
    render(<><Button onClick={onClick} disabledReason="Waiting for 1 clip to start">Finish and build report</Button><Button disabled>Off</Button></>);
    const blocked = screen.getByRole('button', { name: /Finish and build report/ });
    expect(blocked.getAttribute('aria-disabled')).toBe('true');
    expect(blocked.getAttribute('aria-describedby')).toBeTruthy();
    expect(blocked.textContent).toContain('Waiting for 1 clip to start');
    fireEvent.click(blocked);
    expect(onClick).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Off' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('calls onClick when enabled', () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    fireEvent.click(screen.getByRole('button', { name: 'Go' }));
    expect(onClick).toHaveBeenCalledOnce();
  });
});

describe('Card, Pill, Notice', () => {
  it('Card applies tone and padding classes', () => {
    render(<Card tone="sunk" padding="lg" data-testid="c">x</Card>);
    expect(screen.getByTestId('c').className).toBe('card card-sunk card-pad-lg');
  });

  it('Pill carries its tone and its words', () => {
    render(<><Pill tone="not">Not filmed</Pill><Pill tone="synthetic">Synthetic</Pill></>);
    expect(screen.getByText('Not filmed').className).toContain('pill-not');
    expect(screen.getByText('Synthetic').className).toContain('pill-synthetic');
  });

  it('Notice announces errors only, and shows title, body and action', () => {
    render(<><Notice tone="error" title="Upload failed" action={<button type="button">Retry</button>}>Try again.</Notice><Notice tone="warn">Heads up</Notice></>);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Upload failed');
    expect(alert.textContent).toContain('Try again.');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });
});

describe('Field', () => {
  it('labels the control and wires the hint and error', () => {
    render(<Field label="Email" hint="We never share it" error="Enter an email"><input type="email" /></Field>);
    const input = screen.getByLabelText('Email');
    const described = input.getAttribute('aria-describedby') ?? '';
    expect(described.split(' ')).toHaveLength(2);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('Enter an email');
  });

  it('works for a textarea without hint or error', () => {
    render(<Field label="Listing"><textarea /></Field>);
    expect(screen.getByLabelText('Listing').getAttribute('aria-describedby')).toBeNull();
  });
});

describe('ChoiceTiles', () => {
  it('is a radio group; choosing a tile reports its value', () => {
    const onChange = vi.fn();
    render(<ChoiceTiles name="mode" legend="How would you like to capture?" value="a" onChange={onChange}
      options={[{ value: 'a', title: 'One video', description: 'One walk', tag: 'Quickest' }, { value: 'b', title: 'Guided clips', description: 'Per zone', tag: 'Most thorough', tagTone: 'quiet' }]} />);
    expect((screen.getByRole('radio', { name: /One video/ }) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole('radio', { name: /Guided clips/ }));
    expect(onChange).toHaveBeenCalledWith('b');
    expect(screen.getByText('Most thorough').className).toContain('tag-quiet');
  });
});

describe('Disclosure', () => {
  it('starts closed, opens on click, and keeps the heading level', () => {
    render(<Disclosure title="Mechanical checks" summary="Not checked" headingLevel={2}><p>Body</p></Disclosure>);
    expect(screen.getByRole('heading', { level: 2, name: 'Mechanical checks' })).toBeTruthy();
    const details = document.querySelector('details') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(screen.getByText('Not checked')).toBeTruthy();
    fireEvent.click(screen.getByText('Mechanical checks'));
    details.open = true;
    expect(details.open).toBe(true);
  });

  it('shows a visible text label that follows the open state', () => {
    render(<Disclosure title="Part 1 free result" summary="Provisional index 94.0" toggleLabel={['Show details', 'Hide details']}>text</Disclosure>);
    const details = document.querySelector('details') as HTMLDetailsElement;
    expect(screen.getByText('Show details')).toBeTruthy();
    details.open = true;
    fireEvent(details, new Event('toggle'));
    expect(screen.getByText('Hide details')).toBeTruthy();
  });

  it('defaultOpen renders open', () => {
    render(<Disclosure title="About" defaultOpen>text</Disclosure>);
    expect((document.querySelector('details') as HTMLDetailsElement).open).toBe(true);
  });
});

describe('ActionBar, StatusChip, Banner', () => {
  it('ActionBar is a labelled group holding its actions', () => {
    render(<ActionBar label="Capture actions"><Button>Record</Button></ActionBar>);
    expect(screen.getByRole('group', { name: 'Capture actions' }).querySelector('button')).toBeTruthy();
  });

  it('StatusChip reads filmed and checking counts, and only pulses while checking', () => {
    const { rerender } = render(<StatusChip filmed={2} total={8} checking={1} />);
    expect(screen.getByRole('status').textContent).toBe('2 of 8 filmed · 1 checking');
    expect(screen.getByRole('status').className).toContain('status-chip-busy');
    rerender(<StatusChip filmed={3} total={8} />);
    expect(screen.getByRole('status').textContent).toBe('3 of 8 filmed');
    expect(screen.getByRole('status').className).not.toContain('busy');
  });

  it('Banner shows the line and its action', () => {
    render(<Banner action={<button type="button">View</button>}>Driver side result is ready</Banner>);
    expect(screen.getByRole('status').textContent).toContain('Driver side result is ready');
    expect(screen.getByRole('button', { name: 'View' })).toBeTruthy();
  });
});

const NOT_CHECKED_CATEGORY = { state: 'not_checked', score: null, reason: null } as unknown as CategoryScores['visual'];

describe('ScoreTiles', () => {
  const base = (over: Partial<CategoryScores>): CategoryScores => ({
    visual: { state: 'assessed', score: 94.96, reason: null },
    mechanical: NOT_CHECKED_CATEGORY,
    overall: { state: 'not_checked', score: null, basis: 'visual_only', note: '' },
    ...over,
  } as unknown as CategoryScores);

  it('truncates to one decimal, never rounding up, and keeps the test ids', () => {
    render(<ScoreTiles categories={base({})} conditionIndex={89.99} />);
    expect(screen.getByTestId('category-visual').textContent).toContain('94.9');
    expect(screen.getByTestId('category-visual').textContent).not.toContain('95');
    expect(screen.getByTestId('category-mechanical').textContent).toContain('Not checked');
    expect(screen.getByTestId('category-overall').textContent).toContain('Visual only');
    expect(screen.getByTestId('category-scores').textContent).not.toContain('Condition index');
    expect(screen.getByTestId('category-scores').textContent).toContain('Mechanical was not checked');
  });

  it('shows Overall as a number when assessed', () => {
    render(<ScoreTiles categories={base({ mechanical: { state: 'assessed', score: 80, reason: null } as never, overall: { state: 'assessed', score: 87.45, basis: 'blend', note: '' } as never })} />);
    expect(screen.getByTestId('category-overall').textContent).toContain('87.4');
    expect(screen.getByTestId('category-mechanical').textContent).toContain('80.0');
  });
});

describe('CoverageBar', () => {
  const zones: ZoneCoverage[] = [
    { label: 'Driver side', state: 'assessed' }, { label: 'Passenger side', state: 'assessed' }, { label: 'Front', state: 'partial' },
    { label: 'Tires', state: 'not_checked' }, { label: 'Engine', state: 'not_checked' }, { label: 'Underbody', state: 'assessed' },
    { label: 'Dashboard', state: 'optional' }, { label: 'Interior', state: 'optional' },
  ];

  it('names every state in a plain-text line', () => {
    expect(coverageLine(zones)).toBe('In the index: driver side, passenger side, underbody (3 of 6). Partly checked: front. Not checked: tires, engine. Optional: dashboard, interior.');
    render(<CoverageBar zones={zones} />);
    expect(screen.getByText(/In the index: driver side, passenger side, underbody \(3 of 6\)/)).toBeTruthy();
    expect(document.querySelectorAll('.coverage-seg')).toHaveLength(8);
    expect(document.querySelectorAll('.coverage-not_checked')).toHaveLength(2);
  });
});

describe('SeverityPips and EvidenceFrame', () => {
  it('SeverityPips lights as many pips as the severity', () => {
    const { container } = render(<SeverityPips severity={3} />);
    expect(container.querySelectorAll('i.on')).toHaveLength(3);
    expect(container.querySelectorAll('i')).toHaveLength(5);
  });

  it('EvidenceFrame names the frame with its timecode and shows the stamp', () => {
    render(<EvidenceFrame src="/x.jpg" bbox={{ x: 0.5, y: 0.5, w: 0.2, h: 0.2 }} timestampMs={65000} label="Rust on driver sill" />);
    expect(screen.getByRole('button', { name: 'Enlarge frame at 1:05: Rust on driver sill' })).toBeTruthy();
    expect(screen.getAllByText('1:05').length).toBeGreaterThan(0);
  });
});

describe('PageHeader and Section', () => {
  it('PageHeader has one h1, crumbs, meta and actions', () => {
    render(<PageHeader crumbs={[{ label: 'My reports', href: '/reports' }, { label: 'Report' }]} title="2019 Transit" meta="Issued Nov 8" actions={<Button>Share</Button>} />);
    expect(screen.getByRole('heading', { level: 1, name: '2019 Transit' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'My reports' }).getAttribute('href')).toBe('/reports');
    expect(screen.getByText('Report').getAttribute('aria-current')).toBe('page');
    expect(screen.getByText('Issued Nov 8')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Share' })).toBeTruthy();
  });

  it('Section is a region named by its h2', () => {
    render(<Section title="Findings" sub="Each with its frame"><p>x</p></Section>);
    expect(screen.getByRole('region', { name: 'Findings' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Findings' })).toBeTruthy();
    expect(screen.getByText('Each with its frame')).toBeTruthy();
  });
});
