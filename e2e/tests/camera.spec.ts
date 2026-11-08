import { chromium, expect, test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { loadClips, makeY4m } from '../src/clips.js';
import { API, RUN_DIR } from '../src/env.js';
import { openZone, startGuidedInspection, zoneState } from '../src/flow.js';
import { apiGet, finishContext, openContext, readOwner, Recorder, scanForToken, waitForReady } from '../src/run.js';

// Proves the real in-page camera path: Chromium serves a fake camera fed from a clip, and the page records it.
test('camera: record one zone through the in-page recorder', async () => {
  const clips = loadClips();
  const source = clips.zones.find((z) => z.file !== null)?.file;
  if (!source) throw new Error('no zone clip available to feed the fake camera');
  const y4m = makeY4m(source);

  const browser = await chromium.launch({
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', `--use-file-for-fake-video-capture=${y4m}`],
  });
  // Isolate the real recorder from Playwright's recording and tracing overhead.
  const context = await openContext(browser, { permissions: ['camera', 'microphone'] }, false);
  const page = await context.newPage();
  const rec = new Recorder(() => page);
  rec.watch(page);
  let owner: { sessionId: string; token: string } | null = null;
  const part = 1;

  try {
    await rec.step('wait for stack ready', async () => { await waitForReady(); }, { critical: true });
    await rec.step('open app, consent, start inspection', async () => { await startGuidedInspection(page); }, { critical: true });
    await rec.step('read session id and owner token (not stored)', async () => {
      owner = await readOwner(page);
    }, { critical: true });
    await rec.step('open the camera in the page', async () => {
      await openZone(page, part);
      await page.getByRole('button', { name: 'Open camera' }).click();
      await expect(page.getByRole('button', { name: 'Start recording' }), 'step "open camera": the recorder never became ready').toBeVisible({ timeout: 30_000 });
      // Check decoded frames and a live track before proving the MediaRecorder upload below.
      await expect.poll(() => page.locator('video[aria-label^="Live camera preview"]').evaluate((v: HTMLVideoElement) => {
        const tracks = (v.srcObject as MediaStream | null)?.getVideoTracks() ?? [];
        return v.readyState >= 2 && v.videoWidth > 0 && tracks.some((t) => t.readyState === 'live');
      }), { message: 'step "open camera": preview has no live track or data', timeout: 30_000 }).toBe(true);
    }, { critical: true });
    await rec.step('record about 4 seconds and stop', async () => {
      await page.getByRole('button', { name: 'Start recording' }).click();
      await expect(page.getByRole('button', { name: 'Stop recording' })).toBeVisible();
      await expect.poll(async () => Number((await page.getByRole('timer').innerText()).match(/^\d+/)?.[0] ?? 0), {
        message: 'step "record": timer did not reach 4 seconds', timeout: 15_000,
      }).toBeGreaterThanOrEqual(4);
      await page.getByRole('button', { name: 'Stop recording' }).click();
      await expect(page.getByText(/Review your clip/), 'step "record": no review screen after stopping').toBeVisible({ timeout: 30_000 });
      const size = await page.locator('video[aria-label="Preview of your clip"]').evaluate((v: HTMLVideoElement) => v.src.length);
      expect(size).toBeGreaterThan(0);
    }, { critical: true });
    await rec.step('upload the recorded clip and wait for it to process', async (note) => {
      await page.getByRole('button', { name: 'Upload this clip' }).click();
      await expect(zoneState(page, part), 'step "upload recorded clip": never uploaded').toHaveText(/Uploaded|Processing|Processed/, { timeout: 180_000 });
      const deadline = Date.now() + 5 * 60_000;
      let state = '';
      while (Date.now() < deadline) {
        const r = await apiGet(`/sessions/${owner!.sessionId}/progress`, owner!.token);
        state = (JSON.parse(r.body) as { parts: Array<{ part_no: number; state: string }> }).parts.find((p) => p.part_no === part)?.state ?? 'missing';
        if (state === 'processed') break;
        if (state === 'failed') throw new Error('recorded clip failed processing');
        await page.waitForTimeout(2_000);
      }
      expect(state, 'step "upload recorded clip": not processed in time').toBe('processed');
      note(`API ${API}: part ${part} processed`);
    });
  } finally {
    const token = owner ? (owner as { token: string }).token : null;
    try { await finishContext(context, token); } finally { await browser.close(); }
    rec.notes.artifacts = 'screenshots and summary only; camera isolation disables recordVideo and tracing';
    rec.notes.cameraFeed = `fake device fed from ${path.basename(source)} (5 s y4m)`;
    if (token) {
      const leaks = scanForToken(token);
      rec.notes.tokenOnDisk = leaks.length === 0 ? 'none found in run directory' : leaks.map((p) => path.relative(RUN_DIR, p));
      if (leaks.length) rec.steps.push({ n: rec.steps.length + 1, name: 'owner token must not be on disk', status: 'fail', ms: 0, error: 'token found in artifacts' });
    }
    console.log(`summary: ${rec.writeSummary({ runDir: path.basename(RUN_DIR), kind: 'camera' })}`);
    fs.existsSync(RUN_DIR);
  }
  expect(rec.failed().map((s) => `${s.name}: ${s.error?.split('\n')[0]}`), 'failed steps').toEqual([]);
});
