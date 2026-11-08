import { expect, test, type Page } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { loadClips } from '../src/clips.js';
import { buildRunMp4, bundleInputs } from '../src/artifacts.js';
import { API, CACHE_DIR, MODE, POD_PORT, PROCESS_TIMEOUT_MS, PROFILE, REPORT_TIMEOUT_MS, RIG_DIR, RIG_PYTHON, RUN_DIR, SAMPLES_DIR, UPCLOSE_DIR, WEB, ZONES } from '../src/env.js';
import { openZone, startGuidedInspection, uploadZoneClip, zoneState } from '../src/flow.js';
import { apiGet, finishContext, openContext, readOwner, Recorder, scanForToken, waitForReady } from '../src/run.js';

const DTC = /\b[PCBU][0-3][0-9A-F]{3}\b/;
const VIN = /\b[A-HJ-NPR-Z0-9]{17}\b/;

interface Progress { state: string; parts: Array<{ part_no: number; state: string }>; report: null | { slug: string; state: string; unlocked: boolean } }
type Json = Record<string, unknown>;

function textOf(html: string): string {
  return html.replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;|&#160;/g, ' ').replace(/\s+/g, ' ');
}

/** The HTML of the Mechanical checks section only (up to the next top-level section). */
function additionalChecksHtml(html: string): string {
  const start = html.indexOf('id="extra-h"');
  if (start === -1) return '';
  const next = html.indexOf('</section>', start);
  return html.slice(start, next === -1 ? undefined : next);
}

function rigEnv(token: string): NodeJS.ProcessEnv {
  return { ...process.env, UPCLOSE_OWNER_TOKEN: token, PYTHONIOENCODING: 'utf-8' };
}

function runRig(args: string[], token: string, timeoutMs: number): { code: number | null; out: string } {
  const r = spawnSync(RIG_PYTHON, ['-m', 'upclose_rig', ...args], { cwd: RIG_DIR, env: rigEnv(token), encoding: 'utf8', timeout: timeoutMs });
  const out = `${r.stdout ?? ''}${r.stderr ?? ''}`.split(token).join('<token>');
  return { code: r.status, out };
}

test('journey: capture, kits, finalize, report', async ({ browser }) => {
  const clips = loadClips();
  const context = await openContext(browser);
  const page = await context.newPage();
  const rec = new Recorder(() => page);
  rec.watch(page);
  let owner: { sessionId: string; token: string } | null = null;
  const voiceGuideSeen: string[] = [];
  page.on('response', (r) => { if (/voice[-_]?guide/i.test(r.url()) && r.ok()) voiceGuideSeen.push(new URL(r.url()).pathname); });
  const filmed = clips.zones.filter((z) => z.file !== null).map((z) => ZONES.find((x) => x.zone === z.zone)!.part);
  const notFilmed = clips.zones.filter((z) => z.file === null);
  let slug = '';
  const stepR: typeof rec.step = rec.step.bind(rec);
  let audioPath = 'none';
  let rigOutDir: string | null = null;
  let uiAudioFile: string | null = null;
  const podCue = POD_PORT === 'sim' ? '' : 'HUMAN: keep the pod still for 20 s (baseline); once it shows NORMAL, shake or tap it and press the button once';

  const progress = async (): Promise<Progress> => {
    const r = await apiGet(`/sessions/${owner!.sessionId}/progress`, owner!.token);
    if (r.status !== 200) throw new Error(`progress returned HTTP ${r.status}`);
    return JSON.parse(r.body) as Progress;
  };

  try {
    await rec.step('wait for stack ready', async (note) => {
      await waitForReady();
      note(`${API}/ready ok; web ${WEB}`);
    }, { critical: true });

    await rec.step('open app, consent, start inspection', async () => {
      await startGuidedInspection(page);
    }, { critical: true });

    await rec.step('read session id and owner token (not stored)', async (note) => {
      owner = await readOwner(page);
      expect(owner.sessionId).toMatch(/^[0-9a-f-]{36}$/i);
      expect(owner.token.length).toBeGreaterThan(16);
      note(`session ${owner.sessionId}`);
    }, { critical: true });

    await rec.step('capture controls and safe rig token instructions', async () => {
      const kits = page.getByTestId('capture-kits');
      await expect(kits).toBeVisible();
      await kits.getByText('Connect the sensor pod', { exact: true }).click();
      await expect(kits.getByTestId('rig-token-note')).toContainText('UPCLOSE_OWNER_TOKEN');
      // Use a boolean assertion so a failed check cannot print the token into an artifact.
      expect((await kits.innerText()).includes(owner!.token), 'owner token must not render in capture kits').toBe(false);
      await kits.getByText('Connect the sensor pod', { exact: true }).click();
    });

    if (PROFILE === 'wave2') {
      await rec.step('wave2: engine-check guidance controls', async () => {
        await expect(page.getByTestId('engine-check')).toBeVisible();
        await page.getByTestId('engine-check-start').click();
        await expect(page.getByTestId('engine-caption')).toContainText(/pod/i);
        await page.getByTestId('engine-check-next').click();
        await expect(page.getByTestId('engine-caption')).toContainText(/start the engine/i);
        await page.getByTestId('engine-check-next').click();
        await expect(page.getByTestId('engine-check-learn')).toBeVisible();
      });
    }

    for (const z of clips.zones) {
      const meta = ZONES.find((x) => x.zone === z.zone)!;
      if (z.file === null) {
        rec.notChecked(`capture ${z.zone}`, `not filmed on purpose (${z.reason ?? 'not rendered'}); the report must show it as not checked`);
        continue;
      }
      const file = z.file;
      await rec.step(`capture ${z.zone}: upload clip`, async (note) => {
        await uploadZoneClip(page, meta.part, file);
        // The server state leaves "Not filmed yet" once the upload is registered and the tab has finished sending.
        await expect(zoneState(page, meta.part), `step "capture ${z.zone}": upload never finished in the UI`)
          .toHaveText(/Uploaded|Processing|Processed/, { timeout: 180_000 });
        note(path.basename(file));
      }, { critical: true });
    }

    await rec.step('wait for processing of every filmed zone (API progress)', async (note) => {
      const deadline = Date.now() + PROCESS_TIMEOUT_MS;
      let last = '';
      while (Date.now() < deadline) {
        const p = await progress();
        const states = filmed.map((n) => `${n}:${p.parts.find((x) => x.part_no === n)?.state ?? 'missing'}`);
        last = states.join(' ');
        if (last.includes(':failed')) throw new Error(`a zone failed processing: ${last}`);
        if (filmed.every((n) => p.parts.find((x) => x.part_no === n)?.state === 'processed')) { note(last); return; }
        await page.waitForTimeout(2_000);
      }
      throw new Error(`step "wait for processing": not all zones processed within ${PROCESS_TIMEOUT_MS / 1000}s (${last})`);
    }, { critical: true });

    await rec.step('UI shows processed zones', async () => {
      for (const n of filmed) await expect(zoneState(page, n), `step "UI shows processed zones": zone ${n}`).toHaveText('Processed', { timeout: 60_000 });
    });

    // The engine-audio control is part of the capture UI; the audio goes in through it, not through the rig.
    const audioInput = page.getByTestId('engine-audio-file-input');

    await rec.step('kit evidence via rig demo-run (sim pod + emulated OBD)', async (note) => {
      const outDir = path.join(CACHE_DIR, 'rig', owner!.sessionId);
      fs.mkdirSync(outDir, { recursive: true });
      rigOutDir = outDir;
      const base = ['--session', owner!.sessionId, '--api', API];
      const args = ['demo-run', ...base, '--seconds', '60', '--speed', POD_PORT === 'sim' ? '0' : '1', '--port', POD_PORT, '--obd', POD_PORT === 'sim' ? 'dtc:P0301' : 'from-pod', '--out', outDir];
      if (POD_PORT !== 'sim') {
        console.log(`
==================================================
  ${podCue}
  Starting in 10 s on ${POD_PORT}. Keep tapping for the rest of the run.
==================================================`);
        for (let i = 10; i > 0; i--) { console.log(`  ... ${i}`); await new Promise((res) => setTimeout(res, 1_000)); }
      }
      let r = runRig(args, owner!.token, 5 * 60_000);
      if (r.code !== 0 && /invalid choice|unrecognized arguments/i.test(r.out)) {
        note('demo-run unavailable; fell back to simulate/bundle/post and obd-snapshot/post');
        const upr = path.join(outDir, 'sim.upr');
        const obd = path.join(outDir, 'obd.json');
        const a = runRig(['simulate', '--seconds', '60', '--out', upr], owner!.token, 120_000);
        const b = runRig(['post', '--recording', upr, '--provenance', 'synthetic', ...base], owner!.token, 120_000);
        const c = runRig(['obd-snapshot', '--dtc', 'P0301', '--out', obd], owner!.token, 120_000);
        const d = runRig(['post', '--body', obd, ...base], owner!.token, 120_000);
        r = { code: [a, b, c, d].every((x) => x.code === 0) ? 0 : 1, out: [a, b, c, d].map((x) => x.out).join('\n') };
        rec.notes.kitPath = 'bundle/post + obd-snapshot/post';
      } else {
        rec.notes.kitPath = 'demo-run';
      }
      fs.writeFileSync(path.join(RUN_DIR, 'rig-output.txt'), r.out);
      note(`exit ${r.code}`);
      expect(r.code, `step "kit evidence": rig exited ${r.code}; see rig-output.txt\n${r.out.split('\n').slice(-6).join('\n')}`).toBe(0);
    }, { critical: true, caption: podCue || undefined });

    if (rec.steps.some((st) => st.name.startsWith('kit evidence via rig demo-run') && st.status !== 'pass')) {
      await rec.step('OBD snapshot via obd-snapshot + post (demo-run did not finish)', async (note) => {
        const obd = path.join(CACHE_DIR, 'rig', owner!.sessionId, 'obd-fallback.json');
        const a1 = runRig(['obd-snapshot', '--dtc', 'P0301', '--out', obd], owner!.token, 120_000);
        expect(a1.code, `obd-snapshot failed:
${a1.out.slice(-400)}`).toBe(0);
        const a2 = runRig(['post', '--body', obd, '--session', owner!.sessionId, '--api', API], owner!.token, 120_000);
        expect(a2.code, `post failed:
${a2.out.slice(-400)}`).toBe(0);
        note('posted obd_snapshot only');
      });
    }

    const panelKit = async (kit: string): Promise<void> => {
      const check = async (): Promise<void> => {
        const row = page.getByTestId('capture-kits').getByTestId(`kit-${kit}`);
        await expect(row, `step "kits panel": ${kit}`).toContainText('Attached', { timeout: 20_000 });
        await expect(row, `step "kits panel": ${kit} provenance`).toContainText(/Simulated/, { timeout: 5_000 });
      };
      try { await check(); } catch { await page.reload(); await check(); }
    };
    await rec.step('capture-kits panel: phone video attached', async () => {
      await expect(page.getByTestId('capture-kits').getByTestId('kit-phone_video')).toContainText('Attached');
    });
    await rec.step('capture-kits panel: sensor pod (rig_telemetry) attached, Simulated', async () => { await panelKit('rig_telemetry'); });
    await rec.step('capture-kits panel: OBD attached, Simulated', async () => { await panelKit('obd_snapshot'); });

    await rec.step('engine audio through the UI control', async (note) => {
      await expect(page.getByTestId('engine-audio-upload'), 'step "engine audio": upload control missing from the capture UI').toBeVisible({ timeout: 30_000 });
      await expect(audioInput, 'step "engine audio": file input missing').toHaveCount(1);
      // The rig makes a synthetic 16 kHz mono WAV; nothing here is a recording of a real engine.
      const clip = path.join(CACHE_DIR, 'audio', 'engine.wav');
      fs.mkdirSync(path.dirname(clip), { recursive: true });
      const r = runRig(['simulate-audio', '--seconds', '10', '--out', clip], owner!.token, 120_000);
      if (r.code !== 0) throw new Error(`could not make synthetic audio: ${r.out.slice(-300)}`);
      audioPath = 'ui: engine-audio-file-input with rig simulate-audio WAV (synthetic)';
      uiAudioFile = clip;
      // The clip is simulated, so say so before choosing it: a chosen file would otherwise be labelled cached.
      const synthBox = page.getByTestId('engine-audio-synthetic');
      await synthBox.check();
      await expect(synthBox, 'step "engine audio": synthetic checkbox').toBeChecked();
      await audioInput.setInputFiles(clip);
      const rejected = page.getByTestId('audio-rejection');
      await expect.poll(async () => {
        if (await rejected.count()) throw new Error(`engine audio rejected by the UI: ${await rejected.innerText()}`);
        return page.getByTestId('audio-attached').count();
      }, { message: 'step "engine audio": upload confirmation never appeared', timeout: 120_000 }).toBeGreaterThan(0);
      await expect(page.getByTestId('capture-kits').getByTestId('kit-engine_audio'), 'step "engine audio": kit row').toContainText('Attached', { timeout: 60_000 });
      note(audioPath);
      const listed = await apiGet(`/sessions/${owner!.sessionId}/sources`, owner!.token);
      expect(listed.status, 'GET /sources status').toBe(200);
      const srcs = (JSON.parse(listed.body) as { sources?: Array<{ kind: string; provenance: string }> }).sources ?? [];
      const ea = srcs.find((x) => x.kind === 'engine_audio');
      expect(ea, 'GET /sources lists no engine_audio').toBeTruthy();
      expect(ea!.provenance, 'engine_audio provenance').toBe('synthetic');
    }, { critical: true });
    rec.notes.audioPath = audioPath;

    await rec.step('finalize capture', async () => {
      const confirm = page.getByRole('button', { name: 'Yes, finish' });
      if (!(await confirm.isVisible())) await page.getByRole('button', { name: 'Finish capture' }).click();
      await confirm.click();
      await expect(page.getByText('Capture is finished. The selection is frozen.'), 'step "finalize": UI never showed finished').toBeVisible({ timeout: 60_000 });
    }, { critical: true });

    await rec.step('wait for the report', async (note) => {
      const deadline = Date.now() + REPORT_TIMEOUT_MS;
      let last = 'no report yet';
      while (Date.now() < deadline) {
        const p = await progress();
        last = `${p.state} / report ${p.report?.state ?? 'none'}`;
        if (p.report?.state === 'failed') throw new Error(`report build failed (${last})`);
        if (p.report?.state === 'ready') { slug = p.report.slug; note(`slug ${slug}; unlocked=${p.report.unlocked}`); return; }
        await page.waitForTimeout(3_000);
      }
      throw new Error(`step "wait for the report": not ready within ${REPORT_TIMEOUT_MS / 1000}s (${last})`);
    }, { critical: true });

    await stepR('UI says the report is ready', async () => {
      await expect(page.getByText(/Your report is ready/), 'step "UI report ready"').toBeVisible({ timeout: 60_000 });
    });

    // ---- live mode: evidence images appear only after operator approval ----
    if (MODE === 'live') {
      await stepR('operator approval of evidence derivatives', async (note) => {
        const operator = process.env.OPERATOR_TOKEN;
        if (!operator) throw new Error('OPERATOR_TOKEN is not set in the environment');
        const script = path.join(UPCLOSE_DIR, 'scripts', 'approve-derivatives.ps1');
        const run = (extra: string[]): { code: number | null; out: string } => {
          const r = spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-SessionId', owner!.sessionId, ...extra], { cwd: UPCLOSE_DIR, env: process.env, encoding: 'utf8', timeout: 20 * 60_000 });
          return { code: r.status, out: `${r.stdout ?? ''}${r.stderr ?? ''}`.split(operator).join('<operator>').split(owner!.token).join('<token>') };
        };
        const preview = run([]);
        fs.writeFileSync(path.join(RUN_DIR, 'approve-preview.txt'), preview.out);
        expect(preview.code, `approve-derivatives preview failed:
${preview.out.slice(-600)}`).toBe(0);
        const approve = run(['-Approve']);
        fs.writeFileSync(path.join(RUN_DIR, 'approve.txt'), approve.out);
        expect(approve.code, `approve-derivatives -Approve failed:
${approve.out.slice(-600)}`).toBe(0);
        // Keep the previews that were approved, so the run folder shows what became visible.
        fs.mkdirSync(path.join(RUN_DIR, 'approved-previews'), { recursive: true });
        for (const m of preview.out.matchAll(/file=(\S+\.png)/g)) { try { fs.copyFileSync(m[1]!, path.join(RUN_DIR, 'approved-previews', path.basename(m[1]!))); } catch { /* missing preview */ } }
        note(`${(approve.out.match(/APPROVED/g) ?? []).length} derivative(s) approved`);
      }, { critical: true });
    }

    // ---- free view ----
    let unlockedAlready = false;
    let freeJson: Json = {};
    let freeHtml = '';
    await stepR('free view: JSON and HTML hide DTC codes, VIN, times, audio and spectrogram', async (note) => {
      const j = await apiGet(`/reports/${slug}`, owner!.token);
      expect(j.status).toBe(200);
      const json = JSON.parse(j.body) as Json;
      freeJson = json;
      const isFree = 'locked_zones' in json;
      if (!isFree) { unlockedAlready = true; throw new Error('report is already unlocked, so the free view cannot be observed (no free JSON)'); }
      const checks = (json.additional_checks ?? []) as Array<Json>;
      note(`free additional_checks: ${checks.map((c) => `${c.kind}:${c.checked}`).join(', ')}`);
      const blob = JSON.stringify(checks);
      expect(blob, 'free JSON leaks a DTC code').not.toMatch(DTC);
      expect(blob, 'free JSON leaks a VIN').not.toMatch(VIN);
      expect(blob, 'free JSON leaks times, audio or spectrogram').not.toMatch(/"(?:[a-z_]*time[a-z_]*|[a-z_]*_ms|[a-z_]*_at|started[a-z_]*|windows?|audio[a-z_]*|spectrogram[a-z_]*|vin|dtcs?)"\s*:/i);
      expect(JSON.stringify(json), 'free JSON mentions a spectrogram or audio URL').not.toMatch(/spectrogram|audio_url|\.wav|\.mp3/i);
      const h = await apiGet(`${API}/r/${slug}`, owner!.token, 'text/html');
      expect(h.status).toBe(200);
      freeHtml = h.body;
      const section = additionalChecksHtml(h.body);
      expect(section.length, 'free HTML has no Mechanical checks section').toBeGreaterThan(0);
      const plain = textOf(section);
      expect(plain, 'free HTML leaks a DTC code').not.toMatch(DTC);
      expect(textOf(h.body), 'free HTML leaks a VIN').not.toMatch(VIN);
      expect(h.body, 'free HTML has audio or spectrogram').not.toMatch(/<audio|spectrogram|\.wav|\.mp3/i);
      expect(plain, 'free HTML shows capture times').not.toMatch(/\d{1,2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T/);
      note(`HTML additional-checks section ${section ? 'present' : 'absent'}`);
    });
    if (unlockedAlready) rec.notes.freeView = 'report was unlocked before the free view could be read';

    // ---- unlock ----
    await stepR('unlock the full report (demo unlock)', async () => {
      if (!unlockedAlready) {
        await page.goto('/');
        await page.getByRole('button', { name: /Unlock full report/ }).click();
      } else {
        await page.goto(`/report/${slug}`);
      }
      await expect(page.getByTestId('owner-report'), 'step "unlock": report page missing').toBeVisible({ timeout: 60_000 });
      await expect(page.getByTestId('synthetic-badge'), 'step "unlock": paid report did not render').toBeVisible({ timeout: 60_000 });
    }, { critical: true });

    // ---- full report ----
    let paid: Json = {};
    let paidHtml = '';
    await stepR('full report renders', async (note) => {
      const j = await apiGet(`/reports/${slug}`, owner!.token);
      expect(j.status).toBe(200);
      paid = JSON.parse(j.body) as Json;
      expect('locked_zones' in paid, 'report JSON is still the free shape after unlock').toBe(false);
      const h = await apiGet(`${API}/r/${slug}`, owner!.token, 'text/html');
      expect(h.status).toBe(200);
      paidHtml = h.body;
      await expect(page.getByTestId('owner-report').getByRole('heading', { level: 1 })).toBeVisible();
      note(`JSON keys: ${Object.keys(paid).join(',')}`);
    }, { critical: true });

    const kitInReport = async (kind: string): Promise<void> => {
      const checks = (paid.additional_checks ?? []) as Array<{ kind: string; checked: boolean; source?: { provenance?: string } | null }>;
      const row = checks.find((c) => c.kind === kind);
      expect(row, `no ${kind} in additional_checks`).toBeTruthy();
      expect(row!.checked, `${kind} not marked checked`).toBe(true);
      expect(row!.source?.provenance, `${kind} provenance`).toBe('synthetic');
      const section = textOf(additionalChecksHtml(paidHtml));
      expect(section.length, 'HTML has no Mechanical checks section').toBeGreaterThan(0);
      expect(section, `HTML kits section needs a synthetic badge for ${kind}`).toMatch(/synthetic/i);
    };
    await stepR('report kits section: rig_telemetry checked, synthetic badge', async () => { await kitInReport('rig_telemetry'); });
    await stepR('report kits section: obd_snapshot checked, synthetic badge', async () => { await kitInReport('obd_snapshot'); });

    await stepR('zones not captured say not checked', async (note) => {
      const coverage = (paid.coverage ?? []) as Array<{ zone: string; state: string; reason?: string | null }>;
      for (const z of notFilmed) {
        const c = coverage.find((x) => x.zone === z.zone);
        expect(c, `coverage has no entry for ${z.zone}`).toBeTruthy();
        expect(c!.state, `${z.zone} must not read as assessed`).toBe('not_assessed');
      }
      const text = textOf(paidHtml);
      expect(text, 'HTML has no "Not checked" wording').toMatch(/not checked/i);
      const ui = await page.getByTestId('owner-report').innerText();
      expect(ui, 'report page has no "Not checked" wording').toMatch(/not checked/i);
      note(`not filmed: ${notFilmed.map((z) => z.zone).join(', ')}`);
    });

    // ---- wave2 ----
    if (PROFILE === 'wave2') {
      const report = page.getByTestId('owner-report');
      type Cat = { state?: string; score?: number | string | null; basis?: string };
      type Cats = { visual: Cat; mechanical: Cat; overall: Cat };
      const CATEGORY_STATES = ['assessed', 'not_checked'];
      const OVERALL_BASES = ['blend', 'visual_only', 'mechanical_only', 'none'];
      const DASH_STATES = ['assessed', 'not_readable', 'not_checked'];
      const SYNTH_SOURCE = ['synthetic', 'fixture'];
      const SYNTH_TEXT = /Synthetic sample data/;
      const scored = (c: Cat): boolean => c.state === 'assessed' && c.score !== null && c.score !== undefined && Number.isFinite(Number(c.score));
      const str = (v: unknown): string => (typeof v === 'string' ? v : '');
      const dashFilmed = clips.zones.some((z) => z.zone === 'dash_electrical' && z.file !== null);

      await stepR('wave2: Visual, Mechanical and Overall category scores', async (note) => {
        const cats = paid.categories as Cats | undefined;
        expect(cats, 'paid JSON has no categories').toBeTruthy();
        for (const k of ['visual', 'mechanical', 'overall'] as const) {
          expect(CATEGORY_STATES, `categories.${k}.state`).toContain(cats![k].state);
          if (cats![k].state === 'assessed') expect(Number.isFinite(Number(cats![k].score)), `categories.${k} assessed but has no score`).toBe(true);
          else expect(cats![k].score ?? null, `categories.${k} not checked but carries a score`).toBeNull();
        }
        expect(OVERALL_BASES, 'categories.overall.basis').toContain(cats!.overall.basis);
        // Filmed zones plus attached kits: both categories are assessed, so the overall is a blend.
        expect(cats!.visual.state, 'visual should be assessed (zones were filmed)').toBe('assessed');
        expect(cats!.mechanical.state, 'mechanical should be assessed (pod and OBD attached)').toBe('assessed');
        expect(cats!.overall.basis, 'overall basis with both categories assessed').toBe('blend');
        // The page must show what the API says: a rounded number, or the "Not checked" wording.
        const rounded = (c: Cat): string => (scored(c) ? String(Math.round(Number(c.score))) : 'Not checked');
        await expect(report.getByTestId('category-scores'), 'owner report has no category block').toBeVisible();
        await expect(report.getByTestId('category-visual')).toContainText(rounded(cats!.visual));
        await expect(report.getByTestId('category-mechanical')).toContainText(rounded(cats!.mechanical));
        const overallText = scored(cats!.overall) ? rounded(cats!.overall) : cats!.overall.basis === 'visual_only' ? 'Visual only' : 'Not checked';
        await expect(report.getByTestId('category-overall')).toContainText(overallText);
        // Server HTML carries the same three rows.
        for (const row of ['visual', 'mechanical', 'overall']) expect(paidHtml, `server HTML has no ${row} category row`).toContain(`data-category="${row}"`);
        note(`visual ${cats!.visual.state}, mechanical ${cats!.mechanical.state}, overall ${cats!.overall.state} (${cats!.overall.basis})`);
      });

      await stepR('wave2: Mechanical checks panel', async () => {
        await expect(report.getByRole('heading', { name: 'Mechanical checks', level: 2 })).toBeVisible();
        for (const kind of ['rig_telemetry', 'obd_snapshot']) {
          const row = report.getByTestId(`check-${kind}`);
          await expect(row, `Mechanical checks: ${kind} row`).toBeVisible();
          await expect(row.getByText('Checked', { exact: true }), `Mechanical checks: ${kind} should read Checked`).toBeVisible();
        }
        await expect(report.getByTestId('check-engine_audio'), 'Mechanical checks: engine audio row').toBeVisible();
        if (paid.mechanical_breakdown) await expect(report.getByTestId('mechanical-breakdown')).toBeVisible();
        expect(paidHtml, 'server HTML has no Mechanical checks section').toContain('id="extra-h"');
        expect(textOf(additionalChecksHtml(paidHtml)), 'server HTML panel heading').toMatch(/Mechanical checks/);
        expect(textOf(paidHtml), 'the old "Additional checks" name is still shown').not.toMatch(/Additional checks/i);
      });

      await stepR('wave2: vibration chart SVG', async (note) => {
        const vib = paid.vibration as { state?: string; points?: unknown[]; series_source?: string } | null | undefined;
        expect(vib?.state, 'paid JSON vibration.state').toBe('assessed');
        expect((vib?.points ?? []).length, 'vibration points').toBeGreaterThanOrEqual(2);
        const chart = report.getByTestId('vibration-chart');
        await expect(chart).toBeVisible();
        await expect(chart.locator('svg[aria-label="Vibration over time"] path').first()).toBeAttached();
        await expect(chart).toContainText(/Uncalibrated/);
        const at = paidHtml.indexOf('data-testid="vibration-chart"');
        expect(at, 'server HTML has no vibration figure').toBeGreaterThan(-1);
        expect(paidHtml.slice(at, paidHtml.indexOf('</figure>', at)), 'server HTML vibration figure has no <svg>').toMatch(/<svg/);
        note(`${vib!.points!.length} points, source ${vib!.series_source}`);
      });

      await stepR('wave2: dashboard block (free: checked + count; paid: full reading)', async (note) => {
        if (!dashFilmed) { note('dash_electrical was not filmed; dashboard block not required'); return; }
        const free = freeJson.dashboard as Json | null | undefined;
        expect(free, 'free JSON has no dashboard block').toBeTruthy();
        expect(Object.keys(free!).sort(), 'free dashboard must be only checked and flag_count').toEqual(['checked', 'flag_count']);
        expect(typeof free!.checked).toBe('boolean');
        expect(Number.isInteger(free!.flag_count)).toBe(true);
        const dash = paid.dashboard as { assessment?: { state?: string; provenance?: string; lit?: unknown[] } | null; evidence_available?: boolean } | null | undefined;
        expect(dash, 'paid JSON has no dashboard block').toBeTruthy();
        expect(Object.keys(dash!), 'paid dashboard should carry more than the free count').toContain('assessment');
        expect(dash!.evidence_available, 'dashboard crop must stay withheld').toBe(false);
        expect(DASH_STATES, 'dashboard assessment state').toContain(dash!.assessment?.state);
        expect(free!.checked, 'free "checked" must agree with the paid assessment').toBe(dash!.assessment?.state === 'assessed');
        const card = report.getByTestId('dashboard-check');
        await expect(card).toBeVisible();
        await expect(card.getByText(dash!.assessment?.state === 'assessed' ? 'Checked' : 'Not checked', { exact: true })).toBeVisible();
        expect(paidHtml, 'server HTML has no dashboard card').toContain('data-testid="dashboard-check"');
        // The free HTML shows the dashboard only as a Checked / Not checked row with a count.
        expect(textOf(additionalChecksHtml(freeHtml)), 'free HTML dashboard row').toMatch(/Dashboard\s+(Checked|Not checked)\s+\d+/);
        note(`state ${dash!.assessment?.state}, lit ${(dash!.assessment?.lit ?? []).length}, provenance ${dash!.assessment?.provenance}; free checked=${String(free!.checked)} count=${String(free!.flag_count)}`);
      });

      await stepR('wave2: advisory block with its label', async (note) => {
        const adv = paid.advisory as { label?: string; provenance?: string; status?: string } | null | undefined;
        expect(adv, 'paid JSON has no advisory block').toBeTruthy();
        expect(str(adv!.label).length, 'advisory.label is empty').toBeGreaterThan(0);
        const block = report.getByTestId('advisory');
        await expect(block).toBeVisible();
        await expect(block.getByRole('heading', { name: 'Advisory' })).toBeVisible();
        await expect(block, 'advisory label not shown').toContainText(adv!.label!);
        await expect(block).toContainText(/generated advice, not a finding/i);
        if (MODE === 'fixture') {
          expect(SYNTH_SOURCE, 'fixture mode should give fixture advisory').toContain(adv!.provenance);
          await expect(block, 'advisory needs a synthetic badge').toContainText(SYNTH_TEXT);
        }
        const at = paidHtml.indexOf('data-testid="advisory"');
        expect(at, 'server HTML has no advisory section').toBeGreaterThan(-1);
        expect(textOf(paidHtml.slice(at, paidHtml.indexOf('</section>', at))), 'server HTML advisory label').toContain(adv!.label!);
        note(`status ${adv!.status}, provenance ${adv!.provenance}`);
      });

      await stepR('wave2: synthetic badges on simulated evidence', async () => {
        for (const kind of ['rig_telemetry', 'obd_snapshot']) await expect(report.getByTestId(`check-${kind}`), `${kind} needs a synthetic badge`).toContainText(SYNTH_TEXT);
        await expect(report.getByTestId('check-engine_audio'), 'engine_audio needs a synthetic badge').toContainText(/Synthetic sample data|Simulated|Synthetic/i);
        await expect(report.getByTestId('synthetic-badge')).toBeVisible();
        const narr = paid.narration_audio as { provenance?: string } | null | undefined;
        if (narr && SYNTH_SOURCE.includes(str(narr.provenance))) await expect(report.locator('section[aria-label="Spoken report"]'), 'narration needs a synthetic badge').toContainText(SYNTH_TEXT);
        if (dashFilmed) {
          const prov = (paid.dashboard as { assessment?: { provenance?: string } | null } | null)?.assessment?.provenance;
          if (prov && SYNTH_SOURCE.includes(prov)) await expect(report.getByTestId('dashboard-check')).toContainText(SYNTH_TEXT);
        }
        expect(textOf(additionalChecksHtml(paidHtml)), 'server HTML synthetic badge').toMatch(/synthetic|simulated/i);
      });

      await stepR('wave2: engine_bay and underbody read as not checked', async (note) => {
        const coverage = (paid.coverage ?? []) as Array<{ zone: string; state: string }>;
        const unfilmed = ['engine_bay', 'underbody'].filter((zn) => notFilmed.some((z) => z.zone === zn));
        for (const zn of unfilmed) expect(coverage.find((c) => c.zone === zn)?.state, `${zn} coverage`).toBe('not_assessed');
        const missing = report.locator('#r-missing');
        if (unfilmed.includes('engine_bay')) await expect(missing, 'engine bay should be listed under what we could not check').toContainText('Engine bay');
        if (unfilmed.includes('underbody')) await expect(missing).toContainText('Underbody');
        await expect(report.locator('#r-zones tr.not', { hasText: /Engine bay|Underbody/ }).first(), 'All zones table should show them as not checked').toContainText(/Not checked/);
        expect(textOf(paidHtml), 'server HTML wording').toMatch(/not checked/i);
        note(`unfilmed here: ${unfilmed.join(', ') || 'none'}`);
      });

      await stepR('wave2: free view leaks no DTC code, VIN, crop or indicator name (JSON and HTML)', async () => {
        const blobs: Array<[string, string]> = [['free JSON', JSON.stringify(freeJson)], ['free HTML text', textOf(freeHtml)]];
        for (const [name, text] of blobs) {
          expect(text, `${name} leaks a DTC code`).not.toMatch(DTC);
          expect(text, `${name} leaks a VIN`).not.toMatch(VIN);
          expect(text, `${name} leaks a dashboard crop`).not.toMatch(/\bcrop/i);
          expect(text, `${name} leaks a dashboard indicator name`).not.toMatch(/check[_ -]engine|oil[_ -]pressure|\btpms\b|battery[_ -]charging|coolant[_ -]temp|brake[_ -]system/i);
        }
        expect(freeJson.vibration ?? null, 'free JSON carries a vibration series').toBeNull();
        expect(freeJson.advisory ?? null, 'free JSON carries the advisory').toBeNull();
        expect(freeHtml, 'free HTML carries a vibration chart').not.toContain('data-testid="vibration-chart"');
      });

      await stepR('wave2: narration audio element', async () => {
        expect(paid.narration_audio, 'paid JSON has no narration_audio').toBeTruthy();
        await expect(report.locator('.narration-player audio, [data-testid="narration-audio"]').first()).toBeAttached({ timeout: 60_000 });
      });
      await stepR('wave2: engine-audio spectrogram', async () => {
        await expect(report.locator('img[alt*="pectrogram" i]').first(), 'no engine-audio spectrogram image').toBeAttached({ timeout: 60_000 });
      });
      await stepR('wave2: voice-guide catalog loaded during capture', async (note) => {
        expect(voiceGuideSeen.length, 'no successful voice-guide request seen while capturing').toBeGreaterThan(0);
        note(`${voiceGuideSeen.length} request(s), e.g. ${voiceGuideSeen[0]}`);
      });
    } else {
      rec.notChecked('wave2 assertions', 'E2E_PROFILE=wave1');
    }
  } finally {
    const tokenForScrub = owner ? (owner as { token: string }).token : null;
    const videos = await finishContext(context, tokenForScrub);
    rec.notes.mode = MODE;
    rec.notes.podPort = POD_PORT;
    rec.notes.clips = { source: clips.source, dir: clips.dir === path.join(CACHE_DIR, 'clips') ? '.cache/clips' : clips.dir, filmed: filmed.length, notFilmed: notFilmed.map((z) => `${z.zone}: ${z.reason}`) };
    rec.notes.sessionId = (owner as { sessionId: string } | null)?.sessionId ?? null;
    try {
      bundleInputs({ clips: clips.zones, clipSource: clips.source, rigDir: rigOutDir, rigOutput: fs.existsSync(path.join(RUN_DIR, 'rig-output.txt')) ? path.join(RUN_DIR, 'rig-output.txt') : null, audioFile: uiAudioFile, podPort: POD_PORT });
      rec.notes.inputs = 'inputs/INDEX.md';
    } catch (e) { rec.notes.inputs = `bundle failed: ${(e as Error).message}`; }
    try {
      const mp4 = buildRunMp4(videos, rec.steps, podCue);
      rec.notes.runVideo = mp4.file ? 'run.mp4' : 'no page video was recorded';
      rec.notes.runVideoSpeedUps = mp4.speedUps;
    } catch (e) { rec.notes.runVideo = `run.mp4 failed: ${(e as Error).message.slice(0, 300)}`; }
    if (tokenForScrub) {
      const leaks = scanForToken(tokenForScrub).map((p) => path.relative(RUN_DIR, p));
      rec.notes.tokenOnDisk = leaks.length === 0 ? 'none found in run directory' : leaks;
      if (leaks.length) rec.steps.push({ n: rec.steps.length + 1, name: 'owner token must not be on disk', status: 'fail', ms: 0, error: `found in: ${leaks.join(', ')}` });
    }
    const file = rec.writeSummary({ runDir: path.basename(RUN_DIR), mode: MODE });
    console.log(`summary: ${file}`);
  }
  expect(rec.failed().map((s) => `${s.name}: ${s.error?.split('\n')[0]}`), 'failed steps').toEqual([]);
});
