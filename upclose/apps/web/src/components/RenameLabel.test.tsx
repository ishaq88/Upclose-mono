// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApi } from '../api/client';
import { FakeBackend } from '../testing/fakeBackend';
import { RenameLabel } from './RenameLabel';

afterEach(cleanup);

function setup(label: string | null = null) {
  const backend = new FakeBackend();
  const api = createApi({ getToken: () => backend.token, fetchImpl: backend.fetch });
  const onRenamed = vi.fn();
  render(<RenameLabel api={api} sessionId={backend.sessionId} label={label} onRenamed={onRenamed} />);
  return { backend, onRenamed };
}

describe('rename an inspection', () => {
  it('sends the trimmed name to PUT /sessions/{id}/label and reports the stored name', async () => {
    const { backend, onRenamed } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
    fireEvent.change(screen.getByLabelText('Inspection name'), { target: { value: '  2014 Honda Civic ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith('2014 Honda Civic'));
    expect(backend.callsTo('PUT', '/label')[0]?.body).toEqual({ label: '2014 Honda Civic' });
    expect(screen.getByRole('button', { name: 'Rename' })).toBeTruthy();
  });

  it('keeps the form open with a message when the server refuses, and cannot save an empty name', async () => {
    const { backend, onRenamed } = setup('Old name');
    backend.labelStatus = 422;
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
    expect((screen.getByLabelText('Inspection name') as HTMLInputElement).value).toBe('Old name');
    fireEvent.change(screen.getByLabelText('Inspection name'), { target: { value: '' } });
    expect((screen.getByRole('button', { name: 'Save name' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Inspection name'), { target: { value: 'New' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
    await screen.findByRole('alert');
    expect(onRenamed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Rename' })).toBeTruthy();
  });
});
