import type { ReactElement, ReactNode } from 'react';
import { useAccount } from '../accounts/AccountContext';
import { Wordmark } from './Wordmark';

export interface AppHeaderProps {
  /** Before the account links (e.g. a Finish button). */
  actions?: ReactNode;
  /** Between the wordmark and the nav (e.g. a StatusChip). */
  status?: ReactNode;
}

/** App header: wordmark, an optional status slot, then My reports and the account, or Sign in. */
export function AppHeader({ actions, status }: AppHeaderProps): ReactElement {
  const { account } = useAccount();
  return (
    <header className="app-head">
      <div className="wrap">
        <Wordmark />
        {status && <div className="app-status">{status}</div>}
        <nav className="app-nav" aria-label="Account">
          {actions}
          {account ? (
            <>
              <a className="hide-sm" href="/reports">My reports</a>
              <a className="avatar" href="/reports" aria-label={`My reports, signed in as ${account.email}`}>{account.email.slice(0, 1).toUpperCase()}</a>
            </>
          ) : <a href="/signin">Sign in</a>}
        </nav>
      </div>
    </header>
  );
}
