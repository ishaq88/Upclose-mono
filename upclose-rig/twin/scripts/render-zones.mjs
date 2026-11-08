import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, unlink, copyFile } from 'node:fs/promises';
import path from 'node:path';
import ffmpeg from 'ffmpeg-static';
import ffprobe from 'ffprobe-static';
import { openScene } from './browser.mjs';

const output = 'out/zones';
const fps = 30, width = 1280, height = 720;
const zones = ['driver_side', 'passenger_side', 'front_rear_body', 'tires', 'dash_electrical', 'engine_bay', 'underbody', 'interior'];
const unsupported = {
  engine_bay: 'The model has no inspectable engine bay or opening hood. Use the engine capture kits separately.',
  underbody: 'The model has no inspection-grade underbody geometry.',
};
await mkdir(output, { recursive: true });
await unlink(path.join(output, 'manifest.json')).catch(error => { if (error.code !== 'ENOENT') throw error; });
const hash = data => createHash('sha256').update(data).digest('hex');
const config = JSON.parse(await readFile('scene.json', 'utf8'));
const sources = ['scene.json', 'damage.json', 'package-lock.json', 'ASSETS.md', 'src/main.ts', 'src/zones.ts', 'src/cluster.ts', 'src/ground-truth.ts', 'src/types.ts', 'src/textures.ts', 'scripts/browser.mjs', 'scripts/render-zones.mjs', `public${config.model.url}`, `public${config.environment}`];
const hashes = Object.fromEntries(await Promise.all(sources.map(async file => [file, hash(await readFile(file))])));
await copyFile('ASSETS.md', path.join(output, 'ASSETS.md'));
const manifest = { schema_version: 'upclose-twin-zones-v1', provenance: 'synthetic', fps, width, height,
  model_sha256: hashes[`public${config.model.url}`], asset_sha256: Object.fromEntries(Object.entries(hashes).filter(([file]) => file.startsWith('public/'))), input_sha256: hashes, zones: [], variants: [] };
const session = await openScene(true, true);
try {
  const metadata = await session.page.evaluate(() => ({ damage: window.twin.damage, placements: window.twin.placements }));
  const renders = zones.flatMap(zone => zone === 'dash_electrical' ? [{ zone, name: zone, variant: 'warnings' }, { zone, name: `${zone}_clean`, variant: 'clean' }] : [{ zone, name: zone }]);
  for (const { zone, name, variant } of renders) {
    const duration = zone === 'dash_electrical' ? 9 : 12, count = fps * duration;
    if (unsupported[zone]) {
      manifest.zones.push({ zone, status: 'not_rendered', file: null, sha256: null, duration_s: 0, visible_damage_ids: [], reason: unsupported[zone] });
      await writeFile(path.join(output, `ground_truth_${zone}.json`), JSON.stringify({ provenance: 'synthetic', zone, status: 'not_rendered', reason: unsupported[zone], frames: [] }, null, 2));
      continue;
    }
    const file = `${name}.mp4`, video = path.join(output, file), frames = [];
    const encoder = spawn(ffmpeg, ['-y', '-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-framerate', String(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '19', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-metadata', `title=UpClose synthetic ${zone}`, '-metadata', 'comment=SYNTHETIC; Ferrari 458 Italia by vicent091036 CC BY 4.0; Studio Small 09 by Sergej Majboroda / Poly Haven CC0; procedural damage by UpClose.', video], { windowsHide: true, stdio: ['pipe', 'ignore', 'pipe'] });
    let errors = '';
    encoder.stderr.on('data', chunk => { errors += chunk; });
    encoder.stdin.on('error', () => {});
    const completion = new Promise((resolve, reject) => { encoder.on('error', reject); encoder.on('close', code => code === 0 ? resolve() : reject(new Error(errors))); });
    completion.catch(() => {});
    try {
      for (let frame = 0; frame < count; frame++) {
        const result = await session.page.evaluate(({ zone, frame, count, variant }) => {
          const truth = window.twin.capture(frame, count, zone, variant);
          const canvas = document.querySelector('canvas');
          const output = document.createElement('canvas'); output.width = canvas.width; output.height = canvas.height;
          const ctx = output.getContext('2d'); ctx.drawImage(canvas, 0, 0);
          ctx.fillStyle = '#163e3be6'; ctx.fillRect(16, 16, 390, 36);
          ctx.font = 'bold 16px Arial'; ctx.fillStyle = 'white'; ctx.fillText(`SYNTHETIC · ${zone.replaceAll('_', ' ').toUpperCase()}`, 28, 40);
          ctx.fillStyle = '#ffffffed'; ctx.fillRect(0, output.height - 34, output.width, 34);
          ctx.font = '12px Arial'; ctx.fillStyle = '#163e3b';
          ctx.fillText('Ferrari 458 Italia · vicent091036 · CC BY 4.0 | Studio Small 09 · Sergej Majboroda / Poly Haven · CC0', 16, output.height - 13);
          return { truth, jpeg: output.toDataURL('image/jpeg', 0.95).split(',')[1] };
        }, { zone, frame, count, variant });
        frames.push(result.truth);
        if (!encoder.stdin.write(Buffer.from(result.jpeg, 'base64'))) await once(encoder.stdin, 'drain');
        if (frame % 60 === 0) console.log(`${name}: ${frame + 1}/${count}`);
      }
      encoder.stdin.end(); await completion;
    } finally { if (encoder.exitCode === null) encoder.kill(); }
    const probe = JSON.parse(await run(ffprobe.path, ['-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', video]));
    const stream = probe.streams.find(s => s.codec_type === 'video');
    if (stream.codec_name !== 'h264' || stream.width !== width || stream.height !== height || stream.r_frame_rate !== '30/1' || Number(stream.nb_read_frames) !== count || Math.abs(Number(probe.format.duration) - duration) > 0.01) throw new Error(`Bad media format for ${zone}`);
    const receipt = `${name}.ffprobe.json`;
    await writeFile(path.join(output, receipt), JSON.stringify(probe, null, 2));
    const sha256 = hash(await readFile(video));
    const visible = [...new Set(frames.flatMap(f => f.visible_damage.map(d => d.id)))].sort();
    const groundTruth = `ground_truth_${name}.json`;
    const dashTruth = zone === 'dash_electrical' ? { variant, lit_indicators: variant === 'warnings' ? ['check_engine', 'oil_pressure', 'tpms'] : [], tachometer_rpm: 1800, ignition_on: true, engine_running: true, instrument_source: 'Original procedural panel mounted in the model cockpit; warning icons are emissive decals.' } : {};
    await writeFile(path.join(output, groundTruth), JSON.stringify({ schema_version: 'upclose-twin-ground-truth-v1', provenance: 'synthetic', zone, ...dashTruth, video: file, video_sha256: sha256, width, height, fps, duration_seconds: duration, frame_count: count, input_sha256: hashes, ...metadata,
      coordinate_system: 'meters; Y up; front -Z; driver -X; camera local -Z forward/+Y up; matrices column-major',
      bbox_convention: 'xyxy pixels; top-left; exclusive right/bottom; depth-tested visible decal fragments',
      limitations: ['Synthetic appearance only; no real-world accuracy or condition claim.', 'Tire views show model geometry, not measured tread depth.', 'Dashboard lights and gauge values are synthetic fixtures; they do not establish actual electrical or engine state.', 'This two-seat open-top model has no rear seats or headliner to inspect.', 'Dent is shading, not deformation.'], frames }));
    const entry = { zone, status: 'rendered', file, sha256, duration_s: duration, visible_damage_ids: visible, ground_truth: groundTruth, ffprobe: receipt, ...dashTruth };
    if (variant === 'clean') manifest.variants.push(entry); else manifest.zones.push(entry);
    await run(ffmpeg, ['-y', '-hide_banner', '-loglevel', 'error', '-i', video, '-vf', `select=eq(n\\,${Math.floor(count / 2)})`, '-frames:v', '1', path.join(output, `${name}.preview.png`)]);
    if (zone === 'dash_electrical') {
      await mkdir(path.join(output, 'dash_stills'), { recursive: true });
      entry.stills = [];
      for (const frame of [30, 135, 240]) {
        const still = `dash_stills/${name}_${String(frame).padStart(4, '0')}.png`;
        await run(ffmpeg, ['-y', '-hide_banner', '-loglevel', 'error', '-i', video, '-vf', `select=eq(n\\,${frame})`, '-frames:v', '1', path.join(output, still)]);
        entry.stills.push({ file: still, frame, timestamp_s: frame / fps, sha256: hash(await readFile(path.join(output, still))) });
      }
    }
    console.log(`PASS ${name}: H.264 ${width}x${height} ${fps}fps ${duration}s; ${count} decoded frames`);
  }
  if (session.errors.length) throw new Error(session.errors.join('\n'));
  await writeFile(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2));
  console.log(`PASS: manifest includes all ${zones.length} canonical zones. ${output}`);
} finally { await session.close(); }

async function run(binary, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(binary, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', c => { stdout += c; }); child.stderr.on('data', c => { stderr += c; });
    child.on('error', reject); child.on('close', code => code === 0 ? resolve(stdout) : reject(new Error(stderr)));
  });
}
