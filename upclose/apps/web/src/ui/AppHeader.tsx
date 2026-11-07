import type { ReactElement, ReactNode } from 'react';
import { useAccount } from '../accounts/AccountContext';
import { Wordmark } from './Wordmark';

/** App header: wordmark, then My reports and the account, or Sign in. `actions` sits before them (e.g. Finish). */
export function AppHeader({ actions }: { actions?: ReactNode }): ReactElement {
  const { account } = useAccount();
  return (
    <header className="app-head">
      <div className="wrap">
        <Wordmark />
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
