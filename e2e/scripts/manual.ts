import { chromium, type BrowserContext, type Page } from '@playwright/test';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { API, CACHE_DIR, E2E_ROOT, POD_PORT, RIG_DIR, RIG_PYTHON, UPCLOSE_DIR, WEB } from '../src/env.js';
import { startGuidedInspection } from '../src/flow.js';
import { readOwner } from '../src/run.js';

const SELFTEST = process.argv.includes('--selftest');
const stamp = new Date().toISOString().replace(/[:.]/g, '-').replace(/Z$/, 'Z');
const runDir = path.join(E2E_ROOT, 'out', `manual-${stamp}`);
const CUE = 'Keep the pod still 20 s until NORMAL, then shake/tap it and press the button once. 60 s total.';

function scrub(text: string, secrets: Array<string | undefined>): string {
  let t = text;
  for (const s of secrets) if (s && s.length > 8) t = t.split(s).join('<token>');
  return t;
}

async function currentOwner(page: Page): Promise<{ sessionId: string; token: string } | null> {
  try { return await readOwner(page); } catch { return null; }
}

/** Attach pod + OBD evidence with the rig. Streams scrubbed output; returns the full scrubbed text. */
function attachPod(owner: { sessionId: string; token: string }, port: string, quiet: boolean): Promise<{ code: number | null; out: string }> {
  const outDir = path.join(runDir, 'rig');
  fs.mkdirSync(outDir, { recursive: true });
  const args = ['-m', 'upclose_rig', 'demo-run', '--session', owner.sessionId, '--api', API, '--port', port, '--out', outDir];
  if (port === 'sim') args.push('--speed', '0');
  if (!quiet) console.log(`\n==== ${CUE} ====\n`);
  return new Promise((resolve) => {
    const child = spawn(RIG_PYTHON, args, { cwd: RIG_DIR, env: { ...process.env, UPCLOSE_OWNER_TOKEN: owner.token, PYTHONIOENCODING: 'utf-8' } });
    let out = '';
    const onData = (b: Buffer): void => { const s = scrub(b.toString('utf8'), [owner.token]); out += s; process.stdout.write(s); };
    child.stdout.on('data', onData);
    child.stderr.on('data', onData);
    child.on('close', (code) => { fs.writeFileSync(path.join(runDir, 'rig-output.txt'), out); resolve({ code, out }); });
    child.on('error', (e) => { out += String(e); resolve({ code: 1, out }); });
  });
}

function operatorToken(): string | null {
  if (process.env.OPERATOR_TOKEN) return process.env.OPERATOR_TOKEN;
  const f = process.env.OPERATOR_TOKEN_FILE;
  if (f && fs.existsSync(f)) return fs.readFileSync(f, 'utf8').trim();
  return null;
}

function runApprove(sessionId: string, operator: string, owner: string | undefined, approve: boolean): { code: number | null; out: string } {
  const script = path.join(UPCLOSE_DIR, 'scripts', 'approve-derivatives.ps1');
  const extra = approve ? ['-Approve'] : [];
  const r = spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-SessionId', sessionId, ...extra], {
    cwd: UPCLOSE_DIR, env: { ...process.env, OPERATOR_TOKEN: operator }, encoding: 'utf8', timeout: 20 * 60_000,
  });
  return { code: r.status, out: scrub(`${r.stdout ?? ''}${r.stderr ?? ''}`, [operator, owner]) };
}

async function main(): Promise<void> {
  fs.mkdirSync(runDir, { recursive: true });
  const profile = path.join(CACHE_DIR, 'manual-profile');
  fs.mkdirSync(profile, { recursive: true });
  const context: BrowserContext = await chromium.launchPersistentContext(profile, {
    headless: SELFTEST,
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: runDir, size: { width: 1280, height: 720 } },
    baseURL: WEB,
  });
  const page = context.pages()[0] ?? (await context.newPage());
  await page.goto(WEB);
  console.log(`Run folder: ${runDir}`);

  if (SELFTEST) {
    let ok = false;
    let out = '';
    try {
      await startGuidedInspection(page);
      const owner = await currentOwner(page);
      if (!owner) throw new Error('no owner session after start');
      const r = await attachPod(owner, 'sim', true);
      out = r.out;
      ok = r.code === 0 && out.includes('[ OK ] rig_telemetry') && out.includes('[ OK ] obd_snapshot');
    } catch (e) { console.log(scrub(String(e), [])); }
    await context.close();
    console.log(ok ? 'SELFTEST PASS' : 'SELFTEST FAIL');
    process.exit(ok ? 0 : 1);
  }

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const ask = (q: string): Promise<string> => new Promise((res) => rl.question(q, res));
  const menu = '\n[p] attach pod + OBD   [a] approve evidence images   [s] print session id   [q] quit\n> ';
  let quit = false;
  context.on('close', () => { if (!quit) { console.log('Browser closed.'); process.exit(0); } });
  while (!quit) {
    const choice = (await ask(menu)).trim().toLowerCase();
    if (choice === 'p') {
      const owner = await currentOwner(page);
      if (!owner) { console.log('No session found: start an inspection in the browser first.'); continue; }
      const r = await attachPod(owner, POD_PORT, false);
      console.log(`\nrig exited ${r.code}`);
    } else if (choice === 'a') {
      const operator = operatorToken();
      if (!operator) { console.log('Set OPERATOR_TOKEN, or OPERATOR_TOKEN_FILE to a file holding it, then retry.'); continue; }
      const owner = await currentOwner(page);
      if (!owner) { console.log('No session found: start an inspection in the browser first.'); continue; }
      const pv = runApprove(owner.sessionId, operator, owner.token, false);
      console.log(pv.out);
      fs.writeFileSync(path.join(runDir, 'approve-preview.txt'), pv.out);
      for (const m of pv.out.matchAll(/file=(\S+\.png)/g)) console.log(`preview: ${m[1]}`);
      if (pv.code !== 0) { console.log(`preview failed (exit ${pv.code})`); continue; }
      if ((await ask('approve? (y/n) ')).trim().toLowerCase() === 'y') {
        const ap = runApprove(owner.sessionId, operator, owner.token, true);
        console.log(ap.out);
        fs.writeFileSync(path.join(runDir, 'approve.txt'), ap.out);
      }
    } else if (choice === 's') {
      const owner = await currentOwner(page);
      console.log(owner ? `session ${owner.sessionId}` : 'No session yet.');
    } else if (choice === 'q') {
      quit = true;
    }
  }
  rl.close();
  await context.close();
  console.log(`Run folder: ${runDir}`);
  process.exit(0);
}

main().catch((e) => { console.error(e); process.exit(1); });
