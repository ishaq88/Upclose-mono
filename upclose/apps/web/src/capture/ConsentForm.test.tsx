// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AccountProvider } from '../accounts/AccountContext';
import type { Api } from '../api/client';
import { ConsentForm } from '../components/ConsentForm';
import { CONSENT_VERSION } from '../config';

afterEach(cleanup);

function setup(account: { token: string; email: string; expiresAt: string } | null) {
  const createSession = vi.fn(async () => ({ session_id: 'session-1', session_token: 'owner-token' }));
  const onCreated = vi.fn();
  render(<AccountProvider initial={account}><ConsentForm api={{ createSession } as unknown as Api} onCreated={onCreated} /></AccountProvider>);
  return { createSession, onCreated };
}

describe('ConsentForm account link', () => {
  it('passes the account token as the fourth argument and keeps it out of the stored owner session', async () => {
    const { createSession, onCreated } = setup({ token: 'account-token', email: 'a@example.test', expiresAt: '2999-01-01T00:00:00Z' });
    fireEvent.click(screen.getByRole('radio', { name: /Guided clips/ }));
    fireEvent.click(screen.getByLabelText(/I understand and agree/));
    fireEvent.click(screen.getByRole('button', { name: 'Continue with guided clips' }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(createSession).toHaveBeenCalledWith(CONSENT_VERSION, null, 'guided', 'account-token');
    expect(onCreated).toHaveBeenCalledWith({ sessionId: 'session-1', token: 'owner-token' });
    expect(document.body.innerHTML).not.toContain('account-token');
  });

  it('shows provenance-neutral start copy without asserting findings are synthetic', () => {
    setup(null);
    expect(screen.getByText(/Automatic area recognition is not connected — mark sections yourself or use guided clips/)).toBeTruthy();
    expect(screen.getByText(/Your report labels whether findings are synthetic test data or real model output/)).toBeTruthy();
    expect(screen.getByText(/Findings are labeled in the report as synthetic test data or real model output/)).toBeTruthy();
    expect(screen.queryByText(/Current findings are synthetic test data/)).toBeNull();
  });

  it('sends no account token when signed out, and no request at all without consent', async () => {
    const { createSession } = setup(null);
    fireEvent.click(screen.getByRole('button', { name: 'Continue with one video' }));
    expect(createSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText(/I understand and agree/));
    fireEvent.click(screen.getByRole('button', { name: 'Continue with one video' }));
    await waitFor(() => expect(createSession).toHaveBeenCalledWith(CONSENT_VERSION, null, 'single_video', null));
  });
});
