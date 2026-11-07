import { expect, it, vi } from 'vitest';
import { createApi } from './client';

it('retries a lost checkout response with the same key and rotates after a conflict', async () => {
  const keys: string[] = [];
  const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    keys.push(new Headers(init?.headers).get('Idempotency-Key') ?? '');
    if (keys.length === 1) throw new Error('synthetic lost response');
    if (keys.length === 2) return new Response(JSON.stringify({ code: 'STATE_CONFLICT' }), { status: 409 });
    return new Response(JSON.stringify({ checkout_url: 'https://checkout.example.test/synthetic' }), { status: 201 });
  });
  const api = createApi({ getToken: () => 'synthetic-owner', fetchImpl });
  await expect(api.createCheckout('synthetic-report')).rejects.toMatchObject({ status: 0 });
  await expect(api.createCheckout('synthetic-report')).rejects.toMatchObject({ status: 409 });
  await expect(api.createCheckout('synthetic-report')).resolves.toHaveProperty('checkout_url');
  expect(keys[0]).toBeTruthy();
  expect(keys[0]).toBe(keys[1]);
  expect(keys[2]).not.toBe(keys[1]);
});
