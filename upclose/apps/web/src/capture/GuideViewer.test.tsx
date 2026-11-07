// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GUIDE_STEPS } from './guide';
import { GuideViewer } from './GuideViewer';

const mode = vi.hoisted(() => ({ value: 'fail' as 'fail' | 'ready' }));
vi.mock('./Guide3D', async () => {
  const { useEffect } = await import('react');
  return {
    default: ({ onReady, onFail }: { onReady: () => void; onFail: () => void }) => {
      useEffect(() => { if (mode.value === 'fail') onFail(); else onReady(); }, [onReady, onFail]);
      return null;
    },
  };
});

afterEach(cleanup);
const guide = (partNo: number) => GUIDE_STEPS.find((step) => step.partNo === partNo)!;

describe('GuideViewer', () => {
  it('falls back to the text steps when 3D cannot start', async () => {
    mode.value = 'fail';
    render(<GuideViewer guide={guide(1)} />);
    await waitFor(() => expect(screen.getByTestId('guide-fallback')).toBeTruthy());
    expect(screen.getByText(/3D guide is not available/)).toBeTruthy();
    expect(screen.getByText('Start at the rear corner, walk to the front')).toBeTruthy();
  });

  it('shows the walk instruction over the 3D scene once it is ready', async () => {
    mode.value = 'ready';
    render(<GuideViewer guide={guide(2)} />);
    await waitFor(() => expect(screen.queryByText('Loading 3D guide…')).toBeNull());
    expect(screen.getByText('Start at the front corner, walk to the rear')).toBeTruthy();
    expect(screen.getByRole('img', { name: /Passenger side: 3D guide/ })).toBeTruthy();
    expect(screen.queryByTestId('guide-fallback')).toBeNull();
  });
});
