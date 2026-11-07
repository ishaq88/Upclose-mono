interface ImportMetaEnv {
  /** Live app origin; empty while the app isn't public (the landing then runs in coming-soon mode). */
  readonly VITE_APP_ORIGIN: string;
  /** Public source repository, shown while the app isn't public. */
  readonly VITE_REPO_URL: string;
}
