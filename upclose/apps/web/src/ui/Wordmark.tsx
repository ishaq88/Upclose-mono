import type { ReactElement } from 'react';

/** The UpClose wordmark: lowercase name with the orange dot. */
export function Wordmark({ href = '/', small = false }: { href?: string; small?: boolean }): ReactElement {
  return (
    <a className={small ? 'wordmark wordmark-sm' : 'wordmark'} href={href} aria-label="UpClose home">
      upclose<i aria-hidden="true" />
    </a>
  );
}
