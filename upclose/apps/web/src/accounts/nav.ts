/** Full-page navigation, behind an object so tests can replace it (jsdom cannot navigate). */
export const nav = {
  go(path: string): void {
    window.location.assign(path);
  },
};
