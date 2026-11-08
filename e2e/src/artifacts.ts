import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { ffmpeg } from './clips.js';
import { RUN_DIR } from './env.js';
import type { StepResult } from './run.js';

function sha256(file: string): string {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function fontFile(): string {
  for (const p of ['C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf']) {
    if (fs.existsSync(p)) return p;
  }
  throw new Error('no font found for captions');
}

/**
 * One watchable MP4 of the run: every page video joined in order (H.264, yuv420p, 30 fps, faststart),
 * with a caption of the current step and a running clock. Captions follow the first video's timeline.
 */
export interface SpeedUp { step: string; realSeconds: number; speed: number }

/**
 * One watchable MP4 of the run: every page video joined in order (H.264, yuv420p, 30 fps, faststart),
 * with a caption of the current step and a running clock. Waiting steps longer than 20 s are played at 8x
 * with a "8x" caption (single-video runs only). Returns the real durations that were sped up.
 */
export function buildRunMp4(videos: string[], steps: StepResult[], extraCaption = ''): { file: string | null; speedUps: SpeedUp[] } {
  if (videos.length === 0) return { file: null, speedUps: [] };
  const font = fontFile().replace(/\\/g, '/').replace(':', '\\:');
  const capDir = path.join(RUN_DIR, '.captions');
  fs.mkdirSync(capDir, { recursive: true });
  const FAST = 8;
  const timed = steps.filter((s) => s.startMs !== undefined && s.endMs !== undefined);

  // Segments of the first video: [start, end, speed] in real seconds.
  const slow = videos.length === 1
    ? timed.filter((s) => /^wait/i.test(s.name) && (s.endMs! - s.startMs!) > 20_000).map((s) => ({ s, a: s.startMs! / 1000 + 1, b: s.endMs! / 1000 }))
    : [];
  const speedUps: SpeedUp[] = slow.map(({ s }) => ({ step: s.name, realSeconds: Math.round((s.endMs! - s.startMs!) / 1000), speed: FAST }));
  // Map a real time to output time.
  const toOut = (t: number): number => {
    let out = t;
    for (const { a, b } of slow) {
      if (t > a) out -= (Math.min(t, b) - a) * (1 - 1 / FAST);
    }
    return out;
  };

  const draw: string[] = [];
  const common = `fontfile='${font}':fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=8`;
  for (const s of timed) {
    const file = path.join(capDir, `step-${s.n}.txt`);
    fs.writeFileSync(file, `${String(s.n).padStart(2, '0')}  ${s.name}  [${(s.ms / 1000).toFixed(0)}s real, ${s.status.split(' ')[0]}]${s.caption ? `
${s.caption}` : ''}`);
    const t0 = toOut(s.startMs! / 1000).toFixed(2);
    const t1 = (toOut(s.endMs! / 1000) + 0.5).toFixed(2);
    const f = file.replace(/\\/g, '/').replace(':', '\\:');
    draw.push(`drawtext=${common}:textfile='${f}':fontsize=22:x=12:y=h-70:enable='between(t,${t0},${t1})'`);
  }
  for (const { a, b } of slow) {
    const file = path.join(capDir, `fast-${a.toFixed(0)}.txt`);
    fs.writeFileSync(file, `>> ${FAST}x  (waiting, sped up)`);
    draw.push(`drawtext=${common}:textfile='${file.replace(/\\/g, '/').replace(':', '\\:')}':fontsize=30:fontcolor=yellow:x=(w-text_w)/2:y=60:enable='between(t,${toOut(a).toFixed(2)},${toOut(b).toFixed(2)})'`);
  }
  if (extraCaption) {
    const file = path.join(capDir, 'extra.txt');
    fs.writeFileSync(file, extraCaption);
    draw.push(`drawtext=${common}:textfile='${file.replace(/\\/g, '/').replace(':', '\\:')}':fontsize=30:fontcolor=yellow:x=(w-text_w)/2:y=20`);
  }
  draw.push(`drawtext=${common}:text='%{pts\\:hms}':fontsize=20:x=w-tw-12:y=12`);

  const out = path.join(RUN_DIR, 'run.mp4');
  const norm = 'scale=1280:800:force_original_aspect_ratio=decrease,pad=1280:800:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1';
  // One ffmpeg pass per segment (a single graph that trims one input many times buffers the whole video in memory),
  // then a concat of the segments, then one linear caption pass.
  const segDir = path.join(RUN_DIR, '.segments');
  fs.mkdirSync(segDir, { recursive: true });
  const cuts: Array<{ input: string; from: number; to: number | null; speed: number }> = [];
  if (slow.length > 0) {
    let at = 0;
    for (const { a, b } of slow) { cuts.push({ input: videos[0]!, from: at, to: a, speed: 1 }, { input: videos[0]!, from: a, to: b, speed: FAST }); at = b; }
    cuts.push({ input: videos[0]!, from: at, to: null, speed: 1 });
  } else {
    for (const v of videos) cuts.push({ input: v, from: 0, to: null, speed: 1 });
  }
  const segFiles = cuts.map((c, i) => {
    const f = path.join(segDir, `seg-${String(i).padStart(3, '0')}.mp4`);
    ffmpeg(['-ss', String(c.from), ...(c.to === null ? [] : ['-to', String(c.to)]), '-i', c.input,
      '-vf', `${norm},setpts=(PTS-STARTPTS)/${c.speed}`, '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-r', '30', f]);
    return f;
  });
  const list = path.join(segDir, 'list.txt');
  fs.writeFileSync(list, segFiles.map((f) => `file '${f.replace(/\\/g, '/')}'`).join('\n'));
  const joined = path.join(segDir, 'joined.mp4');
  ffmpeg(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', joined]);
  ffmpeg(['-i', joined, '-vf', draw.join(','), '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-r', '30', '-movflags', '+faststart', out]);
  fs.rmSync(segDir, { recursive: true, force: true });
  fs.rmSync(capDir, { recursive: true, force: true });
  return { file: out, speedUps };
}

export interface InputEntry { file: string; kind: string; provenance: string; origin: string; sha256: string }

function readProvenance(file: string, fallback: string): string {
  if (!file.endsWith('.json')) return fallback;
  try {
    const j = JSON.parse(fs.readFileSync(file, 'utf8')) as { provenance?: string };
    return typeof j.provenance === 'string' ? j.provenance : fallback;
  } catch { return fallback; }
}

/**
 * Copies every simulated input into out/<ts>/inputs/ with a manifest and an INDEX.md.
 * Never copies anything outside the listed folders, so no .env or token can end up here.
 */
export function bundleInputs(opts: {
  clips: Array<{ zone: string; file: string | null; reason?: string }>;
  clipSource: string;
  rigDir: string | null;
  rigOutput: string | null;
  audioFile: string | null;
  podPort: string;
}): string {
  const dir = path.join(RUN_DIR, 'inputs');
  fs.mkdirSync(path.join(dir, 'clips'), { recursive: true });
  const entries: InputEntry[] = [];
  const add = (src: string, destRel: string, kind: string, provenance: string, origin: string): void => {
    const dest = path.join(dir, destRel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
    entries.push({ file: destRel.replace(/\\/g, '/'), kind, provenance, origin, sha256: sha256(dest) });
  };

  for (const c of opts.clips) {
    if (c.file) add(c.file, `clips/${path.basename(c.file)}`, `guided clip (${c.zone})`, 'synthetic', opts.clipSource === 'manifest' ? 'twin render, see the clips folder of the twin' : 'ffmpeg testsrc2 placeholder made by this harness');
  }
  const clipManifest = opts.clips.map((c) => ({
    zone: c.zone, status: c.file ? 'filmed' : 'not_filmed', file: c.file ? path.basename(c.file) : null,
    sha256: c.file ? sha256(c.file) : null, provenance: 'synthetic', reason: c.reason ?? null,
  }));
  fs.writeFileSync(path.join(dir, 'clips', 'manifest.json'), JSON.stringify({ source: opts.clipSource, zones: clipManifest }, null, 2));

  const podProv = opts.podPort === 'sim' ? 'synthetic' : 'live (real pod)';
  if (opts.rigDir && fs.existsSync(opts.rigDir)) {
    for (const name of fs.readdirSync(opts.rigDir).sort()) {
      const src = path.join(opts.rigDir, name);
      if (!fs.statSync(src).isFile() || /^\.env|token/i.test(name)) continue;
      let kind = 'rig output';
      let prov = podProv;
      let origin = `rig demo-run (port ${opts.podPort})`;
      if (name.endsWith('.upr')) kind = 'pod recording (.upr)';
      else if (/verdict/i.test(name)) kind = 'anomaly verdict JSON';
      else if (/obd/i.test(name)) { kind = 'OBD snapshot JSON'; prov = 'synthetic'; origin = 'emulated ELM327 adapter, synthetic VIN'; }
      else if (name.endsWith('.wav')) { kind = 'engine audio WAV'; prov = 'synthetic'; origin = 'rig simulate-audio'; }
      else if (/\.wav\.json$/.test(name)) { kind = 'engine audio sidecar'; prov = 'synthetic'; origin = 'rig simulate-audio'; }
      add(src, `rig/${name}`, kind, readProvenance(src, prov), origin);
    }
  }
  if (opts.audioFile && fs.existsSync(opts.audioFile)) {
    add(opts.audioFile, `audio/${path.basename(opts.audioFile)}`, 'engine audio WAV (attached through the UI)', 'synthetic', 'rig simulate-audio or samples folder');
    const side = `${opts.audioFile}.json`;
    if (fs.existsSync(side)) add(side, `audio/${path.basename(side)}`, 'engine audio sidecar', 'synthetic', 'rig simulate-audio');
  }
  if (opts.rigOutput && fs.existsSync(opts.rigOutput)) {
    const lines = fs.readFileSync(opts.rigOutput, 'utf8').split(/\r?\n/).filter((l) => l.trim().startsWith('{'));
    const dest = path.join(dir, 'rig', 'demo-run-steps.jsonl');
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, lines.join('\n') + (lines.length ? '\n' : ''));
    entries.push({ file: 'rig/demo-run-steps.jsonl', kind: 'demo-run JSON step lines', provenance: podProv, origin: 'stdout/stderr of the rig run, token removed', sha256: sha256(dest) });
  }

  const md = [
    '# Inputs of this run',
    '',
    'Everything the app was given. Provenance: synthetic = generated, cached = replay of a real recording, live = captured from a real source now.',
    '',
    '| File | What it is | Provenance | Where it came from | sha256 |',
    '|---|---|---|---|---|',
    ...entries.map((e) => `| ${e.file} | ${e.kind} | ${e.provenance} | ${e.origin} | ${e.sha256.slice(0, 16)}... |`),
    '',
    `Zones not filmed on purpose: ${opts.clips.filter((c) => !c.file).map((c) => `${c.zone} (${c.reason})`).join('; ') || 'none'}`,
    '',
    'The owner token and any .env file are never copied here. Full hashes are in inputs.json.',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(dir, 'INDEX.md'), md);
  fs.writeFileSync(path.join(dir, 'inputs.json'), JSON.stringify(entries, null, 2));
  return dir;
}
