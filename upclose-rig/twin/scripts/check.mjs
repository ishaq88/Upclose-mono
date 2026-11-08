import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { openScene } from './browser.mjs';
import { PNG } from 'pngjs';

await mkdir('out/smoke', { recursive: true });
const session = await openScene();
try {
  const { page, errors } = session;
  const result = await page.evaluate(async () => {
    const { validateDamage } = await import('/src/types.ts');
    const invalid = [null, [{ ...window.twin.damage[0], zone: 'front' }], [{ ...window.twin.damage[0], normal: [0, 0, 0] }], [window.twin.damage[0], window.twin.damage[0]]];
    const failures = invalid.map(input => { try { validateDamage(input); return false; } catch { return true; } });
    return { failures, placements: window.twin.placements, frames: [0, 270, 540, 810, 1024].map(n => window.twin.capture(n, 1080)) };
  });
  assert(result.failures.every(Boolean), 'Invalid damage config must fail');
  assert.equal(result.placements.length, 3);
  const first = result.frames[0].visible_damage.map(d => d.id);
  const opposite = result.frames[2].visible_damage.map(d => d.id);
  assert(first.includes('scratch-driver-door'), 'Driver scratch visible on driver side');
  assert(!first.includes('rust-passenger-quarter'), 'Opposite rust must be depth-occluded');
  assert(opposite.includes('rust-passenger-quarter'), 'Passenger rust visible on passenger side');
  assert(!opposite.includes('scratch-driver-door'), 'Opposite scratch must be depth-occluded');
  assert(!result.frames[4].visible_damage.some(d => d.id === 'rust-passenger-quarter'), 'Exterior rust must not project onto the inner panel visible from the opposite side');
  assert(result.frames.some(f => f.visible_damage.some(d => d.type === 'dent')), 'Hood dent visible');
  for (const frame of result.frames) for (const d of frame.visible_damage) {
    const [x0, y0, x1, y1] = d.bbox_xyxy;
    assert(x0 >= 0 && y0 >= 0 && x1 <= 1920 && y1 <= 1080 && x1 > x0 && y1 > y0 && d.visible_pixels > 0);
  }
  for (const frame of [0, 270, 540]) {
    await page.evaluate(n => window.twin.capture(n, 1080), frame);
    await page.locator('canvas').screenshot({ path: `out/smoke/frame-${frame}.png` });
  }
  assert.deepEqual(errors, []);
  await writeFile('out/smoke/check.json', JSON.stringify(result, null, 2));
  console.log('PASS: 4 invalid-config cases; surface placement; driver/passenger depth occlusion; all 3 damage types visible; bounded boxes; no page errors.');
} finally { await session.close(); }
const viewer = await openScene(false);
try {
  await viewer.page.locator('#markers').uncheck();
  await viewer.page.locator('#markers').check();
  await viewer.page.locator('#damage-list button').first().click();
  assert((await viewer.page.locator('#detail').textContent()).includes('SCRATCH'));
  await viewer.page.locator('#walkaround').click();
  await viewer.page.waitForTimeout(250);
  await viewer.page.locator('#walkaround').click();
  await viewer.page.screenshot({ path: 'out/smoke/viewer.png' });
  assert.deepEqual(viewer.errors, []);
  console.log('PASS: orbit viewer loaded; marker toggle, damage selection, walkaround start/stop; no page errors.');
} finally { await viewer.close(); }

const dash = await openScene(true, true);
try {
  const samples = {};
  for (const variant of ['warnings', 'clean']) {
    const result = await dash.page.evaluate(variant => {
      const truth = window.twin.capture(135, 270, 'dash_electrical', variant);
      return { truth, png: document.querySelector('canvas').toDataURL('image/png').split(',')[1] };
    }, variant);
    assert.deepEqual(result.truth.lit_indicators, variant === 'warnings' ? ['check_engine', 'oil_pressure', 'tpms'] : []);
    assert.equal(result.truth.tachometer_rpm, 1800);
    assert.equal(result.truth.camera.intrinsics.width, 1280);
    assert.equal(result.truth.camera.intrinsics.height, 720);
    const png = PNG.sync.read(Buffer.from(result.png, 'base64'));
    let amber = 0, red = 0;
    for (let i = 0; i < png.data.length; i += 4) {
      const [r, g, b] = png.data.subarray(i, i + 3);
      if (r > 180 && g > 120 && b < 100) amber++;
      if (r > 180 && g < 130 && b < 100) red++;
    }
    samples[variant] = { amber, red };
    await writeFile(`out/smoke/dash-${variant}.png`, Buffer.from(result.png, 'base64'));
  }
  assert(samples.warnings.amber > samples.clean.amber + 300, 'Amber warning lamps must produce visible pixels');
  assert(samples.warnings.red > samples.clean.red + 300, 'Red oil-pressure lamp must produce visible pixels');
  assert.deepEqual(dash.errors, []);
  console.log(`PASS: dashboard lamp truth, running tachometer, 720p calibration, emissive amber/red pixels and clean controls: ${JSON.stringify(samples)}`);
} finally { await dash.close(); }
