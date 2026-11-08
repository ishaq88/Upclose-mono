import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import ffmpegPath from 'ffmpeg-static';
import { CACHE_DIR, CLIPS_DIR, ONLY_ZONES, ZONES } from './env.js';

export interface ZoneClip {
  zone: string;
  /** Absolute path of the clip, or null when the zone is deliberately not filmed. */
  file: string | null;
  reason?: string;
}
export interface ClipSet {
  source: 'manifest' | 'placeholder';
  dir: string;
  zones: ZoneClip[];
}

export function ffmpeg(args: string[]): void {
  if (!ffmpegPath) throw new Error('ffmpeg-static has no binary for this platform; install ffmpeg');
  const r = spawnSync(ffmpegPath, ['-hide_banner', '-loglevel', 'error', '-y', ...args], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`ffmpeg failed: ${r.stderr}`);
}

const PLACEHOLDER_DIR = path.join(CACHE_DIR, 'clips');
// The twin cannot show these zones, so placeholders leave them out too: this keeps the not-checked path exercised.
const PLACEHOLDER_SKIPPED: Record<string, string> = {
  engine_bay: 'placeholder run: engine bay is not filmed',
  underbody: 'placeholder run: underbody is not filmed',
};

function fontFile(): string {
  for (const p of ['C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf']) {
    if (fs.existsSync(p)) return p;
  }
  throw new Error('no font found for drawtext');
}

/** 8 s, 720p synthetic test pattern with burned-in label. Cached on disk. */
export function makePlaceholder(zone: string): string {
  fs.mkdirSync(PLACEHOLDER_DIR, { recursive: true });
  const out = path.join(PLACEHOLDER_DIR, `${zone}.mp4`);
  if (fs.existsSync(out)) return out;
  const font = fontFile().replace(/\\/g, '/').replace(':', '\\:');
  ffmpeg([
    '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30:duration=8',
    '-vf', `drawtext=fontfile='${font}':text='SYNTHETIC PLACEHOLDER ${zone}':x=(w-text_w)/2:y=h-90:fontsize=48:fontcolor=white:box=1:boxcolor=black@0.6`,
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out,
  ]);
  return out;
}

interface ManifestEntry { zone?: string; status?: string; file?: string; reason?: string }

function readManifest(): ManifestEntry[] | null {
  const file = path.join(CLIPS_DIR, 'manifest.json');
  if (!fs.existsSync(file)) return null;
  const raw = JSON.parse(fs.readFileSync(file, 'utf8')) as unknown;
  const list = Array.isArray(raw) ? raw : (raw as { zones?: unknown }).zones;
  if (Array.isArray(list)) return list as ManifestEntry[];
  if (list && typeof list === 'object') {
    return Object.entries(list as Record<string, ManifestEntry>).map(([zone, v]) => ({ ...v, zone: v.zone ?? zone }));
  }
  throw new Error(`manifest.json in ${CLIPS_DIR} has no zones list`);
}

export function loadClips(): ClipSet {
  const set = loadAllClips();
  if (ONLY_ZONES.length === 0) return set;
  const unknown = ONLY_ZONES.filter((z) => !ZONES.some((x) => x.zone === z));
  if (unknown.length) throw new Error(`E2E_ZONES has unknown zones: ${unknown.join(', ')}`);
  return { ...set, zones: set.zones.map((z) => (ONLY_ZONES.includes(z.zone) || z.file === null ? z : { zone: z.zone, file: null, reason: 'excluded by E2E_ZONES' })) };
}

function loadAllClips(): ClipSet {
  const manifest = readManifest();
  if (!manifest) {
    return {
      source: 'placeholder', dir: PLACEHOLDER_DIR,
      zones: ZONES.map((z) => {
        const reason = PLACEHOLDER_SKIPPED[z.zone];
        return reason ? { zone: z.zone, file: null, reason } : { zone: z.zone, file: makePlaceholder(z.zone) };
      }),
    };
  }
  return {
    source: 'manifest', dir: CLIPS_DIR,
    zones: ZONES.map((z) => {
      const entry = manifest.find((m) => m.zone === z.zone);
      if (!entry) return { zone: z.zone, file: null, reason: 'zone missing from manifest' };
      if (entry.status === 'not_rendered' || !entry.file) return { zone: z.zone, file: null, reason: entry.reason ?? 'not_rendered' };
      const file = path.resolve(CLIPS_DIR, entry.file);
      if (!fs.existsSync(file)) throw new Error(`manifest lists ${entry.file} for ${z.zone} but the file is missing`);
      return { zone: z.zone, file };
    }),
  };
}

/** y4m for Chromium's fake camera: 5 s of one clip, scaled down to keep the file small. */
export function makeY4m(clip: string): string {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  const out = path.join(CACHE_DIR, 'camera.y4m');
  ffmpeg(['-i', clip, '-t', '5', '-vf', 'scale=640:360,fps=30', '-pix_fmt', 'yuv420p', out]);
  return out;
}
