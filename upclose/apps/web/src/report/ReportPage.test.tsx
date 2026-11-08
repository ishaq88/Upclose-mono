// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type Api } from '../api/client';
import { ReportPage } from './ReportPage';

// Record which credential the page settles on; the report body itself is covered by OwnerReport's tests.
vi.mock('../components/OwnerReport', () => ({
  OwnerReport: ({ api }: { api: Api & { label?: string } }) => <p data-testid="owner-report">{api.label}</p>,
}));

function fakeApi(label: string, getReport: () => Promise<unknown>): Api {
  return { label, getReport: vi.fn(getReport) } as unknown as Api;
}
const ok = (): Promise<unknown> => Promise.resolve({ state: 'ready' });
const notFound = (): Promise<unknown> => Promise.reject(new ApiError(404, 'NOT_FOUND', 'Not found'));

afterEach(cleanup);

describe('ReportPage credential resolution', () => {
  it('opens a report saved to the account with the account, even when this tab holds another session', async () => {
    const account = fakeApi('account', ok);
    const owner = fakeApi('owner', notFound);
    render(<ReportPage slug="s1" credentials={{ account, owner }} />);
    await waitFor(() => expect(screen.getByTestId('owner-report').textContent).toBe('account'));
  });

  it("falls back to this tab's session for a report that was never saved to the account", async () => {
    const account = fakeApi('account', notFound);
    const owner = fakeApi('owner', ok);
    render(<ReportPage slug="s1" credentials={{ account, owner }} />);
    await waitFor(() => expect(screen.getByTestId('owner-report').textContent).toBe('owner'));
  });

  it('uses the tab credential directly when signed out (owner token or slug-only access)', async () => {
    const owner = fakeApi('owner', ok);
    render(<ReportPage slug="s1" credentials={{ account: null, owner }} />);
    await waitFor(() => expect(screen.getByTestId('owner-report').textContent).toBe('owner'));
    expect(owner.getReport).not.toHaveBeenCalled();
  });

  it('does not switch credentials on errors other than not-owner', async () => {
    const account = fakeApi('account', () => Promise.reject(new ApiError(0, 'NETWORK', 'down')));
    const owner = fakeApi('owner', ok);
    render(<ReportPage slug="s1" credentials={{ account, owner }} />);
    await waitFor(() => expect(screen.getByTestId('owner-report').textContent).toBe('account'));
    expect(owner.getReport).not.toHaveBeenCalled();
  });
});
