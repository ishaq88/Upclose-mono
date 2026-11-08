import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const E2E_ROOT = path.resolve(here, '..');
export const CACHE_DIR = path.join(E2E_ROOT, '.cache');
export const UPCLOSE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'upclose');
export const RIG_DIR = path.resolve(E2E_ROOT, '..', 'upclose-rig');

export const WEB = process.env.E2E_WEB ?? 'http://localhost:5173';
export const API = process.env.E2E_API ?? 'http://localhost:8000';
export const API_V1 = `${API}/api/v1`;
export type Profile = 'wave1' | 'wave2';
export const PROFILE = (process.env.E2E_PROFILE ?? 'wave1') as Profile;
if (PROFILE !== 'wave1' && PROFILE !== 'wave2') throw new Error(`E2E_PROFILE must be wave1 or wave2, got ${PROFILE}`);

export type Mode = 'fixture' | 'live';
export const MODE = (process.env.E2E_MODE ?? 'fixture') as Mode;
if (MODE !== 'fixture' && MODE !== 'live') throw new Error(`E2E_MODE must be fixture or live, got ${MODE}`);
/** Optional subset of zones to film, e.g. E2E_ZONES=driver_side,tires. Others are left unfilmed on purpose. */
export const ONLY_ZONES = (process.env.E2E_ZONES ?? '').split(',').map((z) => z.trim()).filter(Boolean);
/** 'sim' (default) or a serial port such as COM3 for the real Grove pod. */
export const POD_PORT = process.env.E2E_POD_PORT ?? 'sim';
/** Live providers take minutes per clip: processing and report waits scale up. */
export const PROCESS_TIMEOUT_MS = (MODE === 'live' ? 40 : 20) * 60_000;
export const REPORT_TIMEOUT_MS = (MODE === 'live' ? 40 : 8) * 60_000;

export const CLIPS_DIR = path.resolve(process.env.E2E_CLIPS_DIR ?? path.join(RIG_DIR, 'twin', 'out', 'zones'));
export const SAMPLES_DIR = path.resolve(process.env.E2E_SAMPLES_DIR ?? path.join(RIG_DIR, 'samples'));
export const RIG_PYTHON = process.env.E2E_RIG_PYTHON ?? path.join(RIG_DIR, '.venv', 'Scripts', 'python.exe');
export const RUN_DIR = process.env.E2E_RUN_DIR ?? path.join(E2E_ROOT, 'out', 'adhoc');

/** Part numbers are the backend's. Order is the capture screen's display order. */
export const ZONES = [
  { part: 1, zone: 'driver_side', title: 'Driver side' },
  { part: 2, zone: 'passenger_side', title: 'Passenger side' },
  { part: 3, zone: 'front_rear_body', title: 'Front and rear, with glass' },
  { part: 4, zone: 'tires', title: 'Tires' },
  { part: 6, zone: 'engine_bay', title: 'Engine bay' },
  { part: 7, zone: 'underbody', title: 'Underbody, low sweep' },
  { part: 5, zone: 'dash_electrical', title: 'Dashboard' },
  { part: 8, zone: 'interior', title: 'Interior' },
] as const;
