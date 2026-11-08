import { openScene } from './browser.mjs';
import { mkdir, writeFile } from 'node:fs/promises';
const session = await openScene();
try {
  const result = await session.page.evaluate(() => {
    const gl = document.querySelector('canvas').getContext('webgl2');
    const info = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    const start = performance.now();
    for (let frame = 0; frame < 5; frame++) window.twin.capture(frame, 1080);
    return { renderer, milliseconds_for_five_frames: performance.now() - start };
  });
  console.log(JSON.stringify(result));
  if (process.argv.includes('--visibility')) {
    const frames = await session.page.evaluate(() => Array.from({ length: 24 }, (_, index) => window.twin.capture(index * 45, 1080)));
    await mkdir('out/diagnostic', { recursive: true });
    await writeFile('out/diagnostic/visibility.json', JSON.stringify(frames, null, 2));
    await session.page.evaluate(() => window.twin.capture(810, 1080));
    await session.page.locator('canvas').screenshot({ path: 'out/diagnostic/front.png' });
    console.log(JSON.stringify(frames.map(frame => ({ frame: frame.frame, damage: frame.visible_damage }))));
  }
} finally { await session.close(); }
