import { useCallback, useState } from 'react';
import { ApiError, type Api } from '../api/client';
import { describeError, type UiError } from '../errors';
import { defaultNavigate, isSafeCheckoutUrl, rememberCheckout } from './checkout';

export interface UnlockOptions {
  api: Api;
  slug: string;
  navigate?: (url: string) => void;
  /** Called when the server already granted the entitlement (demo stand-in). */
  onUnlocked: () => void;
  onUnauthorized: () => void;
  /** Called after a failed attempt: a 409 means the server's view of the report moved on. */
  onFailed?: () => void;
}

export interface Unlock {
  busy: boolean;
  error: UiError | null;
  start: () => void;
  dismiss: () => void;
}

/** Starts hosted checkout. The response grants nothing; only verified provider state unlocks. */
export function useUnlock({ api, slug, navigate = defaultNavigate, onUnlocked, onUnauthorized, onFailed }: UnlockOptions): Unlock {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<UiError | null>(null);
  const start = useCallback(() => {
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        const result = await api.createCheckout(slug);
        if (result.unlocked) {
          onUnlocked();
          return;
        }
        const checkoutUrl = result.checkout_url ?? '';
        if (!isSafeCheckoutUrl(checkoutUrl)) {
          setError({ title: 'Checkout could not be opened', message: 'The service returned an address that cannot be opened safely. Nothing was charged. Try again later.' });
          return;
        }
        rememberCheckout(slug);
        navigate(checkoutUrl);
      } catch (caught) {
        if (caught instanceof ApiError && (caught.status === 401 || caught.code === 'TOKEN_INVALID')) onUnauthorized();
        setError(describeError(caught));
        onFailed?.();
      } finally {
        setBusy(false);
      }
    })();
  }, [api, slug, navigate, onUnlocked, onUnauthorized, onFailed]);
  const dismiss = useCallback(() => setError(null), []);
  return { busy, error, start, dismiss };
}
