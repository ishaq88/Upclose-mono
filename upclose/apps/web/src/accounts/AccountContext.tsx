import { createContext, useCallback, useContext, useMemo, useState, type ReactElement, type ReactNode } from 'react';
import { createAccountApi, type AccountApi, type AuthResponse } from './api';
import { clearAccount, loadAccount, saveAccount, type StoredAccount } from './storage';

export interface AccountState {
  account: StoredAccount | null;
  api: AccountApi;
  /** Store a fresh sign-in from register or login. */
  signedIn: (auth: AuthResponse) => void;
  /** Revoke on the server when possible, then forget locally either way. */
  signOut: () => Promise<void>;
}

const AccountContext = createContext<AccountState | null>(null);

export function AccountProvider({ children, initial }: { children: ReactNode; initial?: StoredAccount | null }): ReactElement {
  const [account, setAccount] = useState<StoredAccount | null>(() => (initial !== undefined ? initial : loadAccount()));
  const tokenRef = useMemo(() => ({ current: account?.token ?? null }), []); // eslint-disable-line react-hooks/exhaustive-deps
  tokenRef.current = account?.token ?? null;
  const api = useMemo(() => createAccountApi({ getToken: () => tokenRef.current }), [tokenRef]);
  const signedIn = useCallback((auth: AuthResponse) => {
    // Set the token now, not on next render, so a claim issued right after register/login is authenticated.
    tokenRef.current = auth.token;
    const next = { token: auth.token, email: auth.account.email, expiresAt: auth.expires_at };
    saveAccount(next);
    setAccount(next);
  }, [tokenRef]);
  const signOut = useCallback(async () => {
    try { await api.logout(); } catch { /* token may already be invalid; sign out locally regardless */ }
    clearAccount();
    setAccount(null);
  }, [api]);
  const value = useMemo(() => ({ account, api, signedIn, signOut }), [account, api, signedIn, signOut]);
  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>;
}

export function useAccount(): AccountState {
  const value = useContext(AccountContext);
  if (!value) throw new Error('useAccount must be used inside AccountProvider.');
  return value;
}
