export const ZONES = ['driver_side', 'passenger_side', 'front_rear_body', 'tires', 'dash_electrical', 'engine_bay', 'underbody', 'interior'] as const;
export type Vec3 = [number, number, number];
export type Zone = typeof ZONES[number];
export interface Damage { id: string; type: 'scratch' | 'dent' | 'rust'; zone: Zone; position: Vec3; normal: Vec3; size: Vec3 }
export interface FindingPin { id: string; label: string; position: Vec3; zone: Zone; evidenceId: string; verdict: 'confirmed' | 'unverifiable' }
export interface VisibleDamage { id: string; type: Damage['type']; zone: Zone; bbox_xyxy: [number, number, number, number]; visible_pixels: number }
export function validateDamage(input: unknown): Damage[] {
  if (!Array.isArray(input) || input.length > 100) throw new Error('Damage must be an array of at most 100 entries');
  const ids = new Set<string>();
  for (const item of input) {
    if (!item || typeof item.id !== 'string' || !/^[a-z0-9-]+$/.test(item.id) || ids.has(item.id)) throw new Error('Damage IDs must be unique safe strings');
    ids.add(item.id);
    if (!['scratch', 'dent', 'rust'].includes(item.type) || !ZONES.includes(item.zone)) throw new Error(`Invalid damage type/zone: ${item.id}`);
    for (const key of ['position', 'normal', 'size']) {
      if (!Array.isArray(item[key]) || item[key].length !== 3 || !item[key].every((n: unknown) => typeof n === 'number' && Number.isFinite(n))) throw new Error(`Invalid ${key}: ${item.id}`);
    }
    if (item.size.some((n: number) => n <= 0 || n > 5) || Math.hypot(...item.normal) < 0.99 || Math.hypot(...item.normal) > 1.01) throw new Error(`Invalid dimensions/unit normal: ${item.id}`);
  }
  return input as Damage[];
}
