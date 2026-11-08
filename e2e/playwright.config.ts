import { defineConfig } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

function utcStamp(): string {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

// One run directory per invocation. Workers inherit it through the environment.
process.env.E2E_RUN_DIR ??= path.join(here, 'out', utcStamp());

export default defineConfig({
  testDir: path.join(here, 'tests'),
  // Playwright's own scratch (it holds raw traces with request headers) stays outside the run folder.
  outputDir: path.join(here, '.cache', 'pw-output'),
  timeout: (process.env.E2E_MODE === 'live' ? 240 : 25) * 60_000,
  expect: { timeout: 15_000 },
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  // E2E_HEADED=1 shows the browser window and slows actions slightly so a person can follow along. Off by default.
  use: { ...(process.env.E2E_HEADED === '1' ? { headless: false, launchOptions: { slowMo: 150 } } : {}), actionTimeout: 30_000, navigationTimeout: 60_000, baseURL: process.env.E2E_WEB ?? 'http://localhost:5173' },
});
