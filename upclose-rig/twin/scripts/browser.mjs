import path from 'node:path';
import { createServer } from 'vite';

export async function openScene(capture = true, zones = false) {
  process.env.PLAYWRIGHT_BROWSERS_PATH ??= path.resolve('out/.browser-cache');
  const { chromium } = await import('playwright');
  const server = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
  await server.listen();
  console.log(`Local scene server: ${server.resolvedUrls.local[0]}`);
  let browser;
  try {
    const requestedAngle = process.env.TWIN_ANGLE ?? (process.platform === 'win32' ? 'd3d11' : 'default');
    const angle = requestedAngle === 'default' ? [] : [`--use-angle=${requestedAngle}`];
    browser = await chromium.launch({ headless: true, args: ['--enable-webgl', '--ignore-gpu-blocklist', '--enable-unsafe-swiftshader', ...angle] });
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(`${server.resolvedUrls.local[0]}${capture ? `?capture${zones ? '&zones' : ''}` : ''}`);
    console.log('Browser loaded; waiting for car and HDRI.');
    await page.waitForFunction(() => window.twin?.ready || window.twinError, undefined, { timeout: 90000 });
    const failure = await page.evaluate(() => window.twinError);
    if (failure) throw new Error(failure);
    console.log(`Scene ready (${capture ? 'capture' : 'viewer'}).`);
    return { page, errors, close: async () => { await browser.close(); await server.close(); } };
  } catch (error) { await browser?.close(); await server.close(); throw error; }
}
