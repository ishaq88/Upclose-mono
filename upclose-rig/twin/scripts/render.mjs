import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, stat, copyFile } from 'node:fs/promises';
import path from 'node:path';
import ffmpeg from 'ffmpeg-static';
import { PNG } from 'pngjs';
import { openScene } from './browser.mjs';

const args = process.argv.slice(2);
function option(name, fallback) { const i = args.indexOf(name); return i === -1 ? fallback : args[i + 1]; }
const id = option('--id', `walkaround-${new Date().toISOString().replace(/[:.]/g, '-')}`);
if (!/^[a-zA-Z0-9_-]+$/.test(id)) throw new Error('Render ID must contain only letters, digits, hyphens, underscores');
const config = JSON.parse(await readFile('scene.json', 'utf8'));
const seconds = Number(option('--seconds', config.durationSeconds));
if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 45 || !Number.isInteger(seconds * config.fps)) throw new Error('Duration must be positive, at most 45 seconds and an integer frame count');
const frameCount = seconds * config.fps;
const directory = path.join('out', id);
await mkdir('out', { recursive: true });
await mkdir(directory); // Never overwrite an earlier evidence run.
await mkdir(path.join(directory, 'check'));
const sha256 = data => createHash('sha256').update(data).digest('hex');
const inputs = ['scene.json', 'damage.json', 'package-lock.json', 'ASSETS.md', 'src/main.ts', 'src/zones.ts', 'src/cluster.ts', 'src/textures.ts', 'src/ground-truth.ts', 'src/types.ts', 'scripts/render.mjs', 'scripts/browser.mjs', `public${config.model.url}`, `public${config.environment}`];
const hashes = Object.fromEntries(await Promise.all(inputs.map(async file => [file, sha256(await readFile(file))])));
for (const file of ['scene.json', 'damage.json', 'ASSETS.md']) await copyFile(file, path.join(directory, file));
const session = await openScene();
const graphicsRenderer = await session.page.evaluate(() => {
  const gl = document.querySelector('canvas').getContext('webgl2');
  const info = gl.getExtension('WEBGL_debug_renderer_info');
  return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
});
let encoder;
let encoderError = '';
const frames = [];
try {
  encoder = spawn(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-framerate', String(config.fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '19', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-metadata', 'title=UpClose synthetic digital twin', '-metadata', 'comment=SYNTHETIC. Ferrari 458 Italia by vicent091036 (CC BY 4.0); Studio Small 09 by Sergej Majboroda / Poly Haven (CC0); procedural damage by UpClose.', path.join(directory, 'walkaround.synthetic.mp4')], { windowsHide: true, stdio: ['pipe', 'ignore', 'pipe'] });
  const completion = new Promise((resolve, reject) => { encoder.on('error', reject); encoder.on('close', code => code === 0 ? resolve() : reject(new Error(`ffmpeg exit ${code}: ${encoderError}`))); });
  completion.catch(() => {});
  encoder.stderr.on('data', chunk => { encoderError += chunk; });
  encoder.stdin.on('error', () => {});
  for (let frame = 0; frame < frameCount; frame++) {
    const capture = await session.page.evaluate(({ frame, frameCount }) => {
      const truth = window.twin.capture(frame, frameCount);
      const canvas = document.querySelector('canvas');
      // Burn provenance into the evidence frame without covering the car or changing the ID pass.
      const output = document.createElement('canvas'); output.width = canvas.width; output.height = canvas.height;
      const context = output.getContext('2d'); context.drawImage(canvas, 0, 0);
      context.fillStyle = '#163e3be6'; context.fillRect(24, 24, 280, 38);
      context.font = 'bold 16px Arial'; context.fillStyle = '#ffffff'; context.fillText('SYNTHETIC · UPCLOSE TWIN', 38, 49);
      context.font = '12px Arial'; context.fillStyle = '#36504d'; context.fillText('Ferrari 458 Italia · vicent091036 · CC BY 4.0 | Studio Small 09 · Poly Haven · CC0', 26, output.height - 22);
      return { truth, jpeg: output.toDataURL('image/jpeg', 0.95).split(',')[1] };
    }, { frame, frameCount });
    frames.push(capture.truth);
    if (!encoder.stdin.write(Buffer.from(capture.jpeg, 'base64'))) await once(encoder.stdin, 'drain');
    if (frame % 30 === 0) console.log(`Captured ${frame + 1}/${frameCount}`);
  }
  encoder.stdin.end(); await completion;
  if (session.errors.length) throw new Error(session.errors.join('\n'));
  const video = path.join(directory, 'walkaround.synthetic.mp4');
  const bytes = (await stat(video)).size;
  if (bytes >= 200 * 1024 * 1024) throw new Error('Video exceeds 200 MiB');
  const truth = { schema_version: 'upclose-twin-ground-truth-v1', provenance: 'synthetic', render_id: id,
    video: 'walkaround.synthetic.mp4', video_sha256: sha256(await readFile(video)), video_bytes: bytes,
    fps: config.fps, width: config.width, height: config.height, frame_count: frameCount, duration_seconds: seconds,
    input_sha256: hashes, graphics_renderer: graphicsRenderer, coordinate_system: 'meters; Y up; front -Z; driver -X; camera local -Z forward/+Y up; matrices column-major',
    bbox_convention: 'xyxy pixels, top-left origin, exclusive right/bottom; visible alpha>=0.10 fragments from full-resolution depth-tested ID pass; no MSAA in ID pass',
    occlusion: 'Car and ground write opaque depth, including glass conservatively. Marker/pin groups excluded. Zero visible pixels => item omitted, not clean.',
    limitations: ['Synthetic decal visibility is not real-world detection accuracy.', 'Dent is a shading decal, not geometric deformation.', 'ID alpha threshold excludes faint decal edges; color pass is antialiased.', 'Exterior camera does not assess interior, engine, dashboard or underbody.'],
    damage: await session.page.evaluate(() => window.twin.damage), placements: await session.page.evaluate(() => window.twin.placements), frames };
  await writeFile(path.join(directory, 'ground_truth.json'), JSON.stringify(truth));
  const verification = await verifyVideo(video, frameCount);
  await writeFile(path.join(directory, 'check', 'video-verification.txt'), verification);
  const chosen = new Set();
  for (const item of truth.damage) {
    const ranked = frames.filter(frame => frame.visible_damage.some(d => d.id === item.id)).sort((a, b) => b.visible_damage.find(d => d.id === item.id).visible_pixels - a.visible_damage.find(d => d.id === item.id).visible_pixels);
    const best = ranked.find(frame => !chosen.has(frame.frame));
    if (!best) throw new Error(`No visible frame for ${item.id}`);
    chosen.add(best.frame);
    const filename = path.join(directory, 'check', `frame-${String(best.frame).padStart(4, '0')}`);
    await runFFmpeg(['-i', video, '-vf', `select=eq(n\\,${best.frame})`, '-frames:v', '1', `${filename}.png`]);
    const png = PNG.sync.read(await readFile(`${filename}.png`));
    for (const d of best.visible_damage) drawBox(png, d.bbox_xyxy);
    await writeFile(`${filename}.overlay.png`, PNG.sync.write(png));
    await writeFile(`${filename}.json`, JSON.stringify(best, null, 2));
  }
  await writeFile(path.join(directory, 'check', 'summary.json'), JSON.stringify({ passed: true, frame_count: frameCount, bytes, overlay_frames: [...chosen], checks: ['decoded frame count', '1920x1080', '30 fps', 'under 200 MiB', 'all configured damage visible at least once'] }, null, 2));
  console.log(`PASS: ${frameCount} matched frames; ${(bytes / 1048576).toFixed(2)} MiB; 3 overlay checks. Output: ${directory}`);
} finally { if (encoder && encoder.exitCode === null) encoder.kill(); await session.close(); }

async function runFFmpeg(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(ffmpeg, ['-hide_banner', ...args], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
    let output = ''; child.stderr.on('data', chunk => { output += chunk; });
    child.on('error', reject); child.on('close', code => code === 0 ? resolve(output) : reject(new Error(output)));
  });
}
async function verifyVideo(video, expected) {
  const result = await runFFmpeg(['-i', video, '-map', '0:v:0', '-f', 'null', '-']);
  const counts = [...result.matchAll(/frame=\s*(\d+)/g)];
  if (Number(counts.at(-1)?.[1]) !== expected || !result.includes('1920x1080') || !result.includes('30 fps')) throw new Error(`Video verification failed: ${result}`);
  return result;
}
function drawBox(png, [x0, y0, x1, y1]) {
  for (let y = Math.max(0, y0 - 2); y < Math.min(png.height, y1 + 2); y++) for (let x = Math.max(0, x0 - 2); x < Math.min(png.width, x1 + 2); x++) {
    if (x >= x0 + 2 && x < x1 - 2 && y >= y0 + 2 && y < y1 - 2) continue;
    const i = (y * png.width + x) * 4; png.data[i] = 230; png.data[i + 1] = 79; png.data[i + 2] = 35; png.data[i + 3] = 255;
  }
}
