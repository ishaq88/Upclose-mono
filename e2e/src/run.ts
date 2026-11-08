import { unzipSync, zipSync } from 'fflate';
import fs from 'node:fs';
import path from 'node:path';
import type { Browser, BrowserContext, Page } from '@playwright/test';
import { API, API_V1, PROFILE, RUN_DIR } from './env.js';

export type StepStatus = 'pass' | 'fail' | 'skipped' | 'not-checked';
export interface StepResult { n: number; name: string; status: StepStatus; ms: number; startMs?: number; endMs?: number; caption?: string; screenshot?: string; note?: string; error?: string }

export interface StepOptions {
  /** If this step fails, later steps that depend on it are skipped. */
  critical?: boolean;
  /** Extra text for the video caption, e.g. a cue for the human. */
  caption?: string;
}

/** Records named steps with timings and a screenshot each. Every assertion failure fails the run. */
export class Recorder {
  readonly steps: StepResult[] = [];
  readonly notes: Record<string, unknown> = {};
  private blockedBy: string | null = null;
  private counter = 0;
  /** Recent failed requests, HTTP errors and console errors (no headers, no tokens). */
  readonly diag: string[] = [];
  watch(page: Page): void {
    const clean = (u: string): string => u.replace(/[0-9a-f]{8}-[0-9a-f-]{27}/g, '<id>').split('?')[0]!;
    page.on('requestfailed', (r) => this.diag.push(`requestfailed ${r.method()} ${clean(r.url())} ${r.failure()?.errorText ?? ''}`));
    page.on('response', (r) => { if (r.status() >= 400) this.diag.push(`HTTP ${r.status()} ${r.request().method()} ${clean(r.url())}`); });
    page.on('console', (m) => { if (m.type() === 'error') this.diag.push(`console ${m.text().slice(0, 160)}`); });
  }
  /** Video time zero: the moment the page was created. */
  readonly origin = Date.now();

  constructor(private readonly page: () => Page | null, private readonly dir = path.join(RUN_DIR, 'screenshots')) {
    fs.mkdirSync(this.dir, { recursive: true });
  }

  async step(name: string, fn: (note: (text: string) => void) => Promise<void>, opts: StepOptions = {}): Promise<void> {
    const n = ++this.counter;
    if (this.blockedBy) {
      this.steps.push({ n, name, status: 'skipped', ms: 0, note: `blocked by failed step "${this.blockedBy}"` });
      return;
    }
    const started = Date.now();
    const notes: string[] = [];
    let status: StepStatus = 'pass';
    let error: string | undefined;
    try {
      await fn((t) => notes.push(t));
    } catch (caught) {
      error = (caught instanceof Error ? caught.message : String(caught)).split('\n').slice(0, 12).join('\n');
      status = 'fail';
      if (opts.critical) this.blockedBy = name;
    }
    if (error) {
      const page = this.page();
      try {
        const banners = page && !page.isClosed() ? await page.locator('[role=alert], .notice-error, [role=status]').allInnerTexts() : [];
        const text = banners.map((b) => b.replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 6);
        if (text.length) error += `
PAGE BANNERS: ${text.join(' || ')}`;
      } catch { /* page gone */ }
      if (this.diag.length) error += `
RECENT NETWORK/CONSOLE: ${this.diag.slice(-8).join(' || ')}`;
    }
    const shot = await this.shot(n, name);
    this.steps.push({ n, name, status, ms: Date.now() - started, startMs: started - this.origin, endMs: Date.now() - this.origin, caption: opts.caption, screenshot: shot, note: notes.length ? notes.join(' | ') : undefined, error });
    console.log(`  [${String(n).padStart(2, '0')}] ${status.padEnd(8)} ${name} (${((Date.now() - started) / 1000).toFixed(1)}s)${error ? `\n        ${error.split('\n')[0]}` : ''}`);
  }

  /** A check that was deliberately not made. It is reported, never counted as a pass. */
  notChecked(name: string, why: string): void {
    const n = ++this.counter;
    this.steps.push({ n, name, status: 'not-checked', ms: 0, note: why });
    console.log(`  [${String(n).padStart(2, '0')}] not-checked ${name}: ${why}`);
  }

  private async shot(n: number, name: string): Promise<string | undefined> {
    const page = this.page();
    if (!page || page.isClosed()) return undefined;
    const file = `${String(n).padStart(2, '0')}-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60)}.png`;
    try {
      await page.screenshot({ path: path.join(this.dir, file), fullPage: true, timeout: 10_000 });
      return path.join('screenshots', file);
    } catch {
      return undefined;
    }
  }

  failed(): StepResult[] { return this.steps.filter((s) => s.status === 'fail'); }

  writeSummary(extra: Record<string, unknown>): string {
    const counts: Record<string, number> = {};
    for (const s of this.steps) {
      const key = s.status;
      counts[key] = (counts[key] ?? 0) + 1;
    }
    const file = path.join(RUN_DIR, 'summary.json');
    fs.writeFileSync(file, JSON.stringify({
      profile: PROFILE, result: this.failed().length === 0 ? 'pass' : 'fail', counts,
      finishedAt: new Date().toISOString(), ...extra, notes: this.notes, steps: this.steps,
    }, null, 2));
    return file;
  }
}

/** Blocks until the API reports ready. The stack may be restarting while the harness runs. */
export async function waitForReady(timeoutMs = 10 * 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let last = 'no response';
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${API}/ready`, { signal: AbortSignal.timeout(30_000) });
      if (r.ok) return;
      last = `HTTP ${r.status}`;
    } catch (e) {
      last = (e as Error).message;
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  throw new Error(`step "wait for stack": ${API}/ready not ready after ${timeoutMs / 1000}s (last: ${last})`);
}

export async function apiGet(pathAndQuery: string, token: string, accept = 'application/json'): Promise<{ status: number; body: string }> {
  const url = pathAndQuery.startsWith('http') ? pathAndQuery : `${API_V1}${pathAndQuery}`;
  let lastError = '';
  // The API can be slow or restarting while the pipeline works: retry transport failures and 502/503/504.
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const r = await fetch(url, { headers: { Authorization: `Bearer ${token}`, Accept: accept }, signal: AbortSignal.timeout(90_000) });
      const body = await r.text();
      if (![502, 503, 504].includes(r.status)) return { status: r.status, body };
      lastError = `HTTP ${r.status}`;
    } catch (e) {
      lastError = (e as Error).message;
    }
    await new Promise((res) => setTimeout(res, 3_000 * attempt));
  }
  throw new Error(`GET ${url.replace(/[0-9a-f-]{36}/g, '<id>')} failed after retries: ${lastError}`);
}

/** Context with video and a trace running. Close it with finishContext. */
export async function openContext(browser: Browser, extra: Parameters<Browser['newContext']>[0] = {}, artifacts = true): Promise<BrowserContext> {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    ...(artifacts ? { recordVideo: { dir: path.join(RUN_DIR, 'video-raw'), size: { width: 1280, height: 800 } } } : {}),
    ...extra,
  });
  if (artifacts) await context.tracing.start({ screenshots: true, snapshots: true });
  return context;
}

/** Stops the trace, closes the context, keeps every page video, and removes the owner token from the trace. Returns the raw webm paths in page order. */
export async function finishContext(context: BrowserContext, token: string | null): Promise<string[]> {
  const tracePath = path.join(RUN_DIR, 'trace.zip');
  await context.tracing.stop({ path: tracePath }).catch(() => undefined);
  const videos = context.pages().map((p) => p.video()).filter((v): v is NonNullable<typeof v> => v !== null);
  await context.close().catch(() => undefined);
  const kept: string[] = [];
  for (const [i, video] of videos.entries()) {
    try {
      const dest = path.join(RUN_DIR, `video-${i + 1}.webm`);
      fs.renameSync(await video.path(), dest);
      kept.push(dest);
    } catch { /* video missing */ }
  }
  fs.rmSync(path.join(RUN_DIR, 'video-raw'), { recursive: true, force: true });
  if (token) redactTrace(tracePath, token);
  return kept;
}

/** The trace records request headers and storage. Replace the token, byte for byte, in every entry. */
function redactTrace(file: string, token: string): void {
  if (!fs.existsSync(file)) return;
  const needle = Buffer.from(token);
  const blank = Buffer.alloc(needle.length, 'x');
  const entries = unzipSync(new Uint8Array(fs.readFileSync(file)));
  const out: Record<string, Uint8Array> = {};
  for (const [name, data] of Object.entries(entries)) {
    const buf = Buffer.from(data);
    let at = buf.indexOf(needle);
    while (at !== -1) { blank.copy(buf, at); at = buf.indexOf(needle, at + needle.length); }
    out[name] = new Uint8Array(buf);
  }
  fs.writeFileSync(file, zipSync(out, { level: 6 }));
}

/** Returns the artifact paths that still contain the token. An empty list means none do. */
export function scanForToken(token: string, root = RUN_DIR): string[] {
  const needle = Buffer.from(token);
  const hits: string[] = [];
  const walk = (dir: string): void => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      const data = fs.readFileSync(p);
      if (data.includes(needle)) { hits.push(p); continue; }
      if (p.endsWith('.zip')) {
        for (const inner of Object.values(unzipSync(new Uint8Array(data)))) {
          if (Buffer.from(inner).includes(needle)) { hits.push(p); break; }
        }
      }
    }
  };
  walk(root);
  return hits;
}

export async function readOwner(page: Page): Promise<{ sessionId: string; token: string }> {
  const raw = await page.evaluate(() => window.sessionStorage.getItem('upclose.session.v1'));
  if (!raw) throw new Error('no owner session in sessionStorage (key upclose.session.v1)');
  const parsed = JSON.parse(raw) as { sessionId?: string; token?: string };
  if (!parsed.sessionId || !parsed.token) throw new Error('stored owner session has no sessionId/token');
  return { sessionId: parsed.sessionId, token: parsed.token };
}
