// Pure data for the capture guide: display order, walk instructions and where each zone sits on the car.
// No three.js import here, so the capture screens and their tests can use it without loading 3D.

/** Display order only. Part numbers stay as the backend defines them; the optional zones come last. */
export const DISPLAY_ORDER: readonly number[] = [1, 2, 3, 4, 6, 7, 5, 8];
export const OPTIONAL_PARTS: ReadonlySet<number> = new Set([5, 8]);

export function sortForDisplay<T extends { part_no: number }>(parts: readonly T[]): T[] {
  const rank = (partNo: number): number => {
    const index = DISPLAY_ORDER.indexOf(partNo);
    return index === -1 ? DISPLAY_ORDER.length + partNo : index;
  };
  return [...parts].sort((a, b) => rank(a.part_no) - rank(b.part_no));
}

/** One-line instruction shown on the 3D guide. Underbody never asks anyone to go under the vehicle. */
export const WALK_TEXT: Readonly<Record<number, string>> = {
  1: 'Start at the rear corner, walk to the front',
  2: 'Start at the front corner, walk to the rear',
  3: 'Arc around the front, then around the rear',
  4: 'Stop at each tire for 3 to 5 seconds',
  5: 'From the driver’s seat, hold steady on the dash',
  6: 'Hood open, film from the front',
  7: 'Stay standing outside, phone low, sweep along the side',
  8: 'Through the open doors',
};

export function walkText(partNo: number): string {
  return WALK_TEXT[partNo] ?? WALK_TEXT[1]!;
}

export type Vec3 = readonly [number, number, number];
/** Box centre and size, [x, y, z, width, height, depth]. */
export type BoxSpec = readonly [number, number, number, number, number, number];

export interface CarDims { hx: number; hz: number; h: number }
export interface ZoneSpec {
  /** Direction from the car toward the camera. */
  dir: Vec3;
  pad?: number;
  boxes: readonly BoxSpec[];
  paths: readonly (readonly Vec3[])[];
  wheels?: boolean;
  /** Draw as an outline that shows through the body instead of a glowing slab. */
  onTop?: boolean;
}

/** Car space after centring: front = +z, driver side = +x, ground = y 0. hx/hz are half width/length, h the height. */
export function zoneSpec(partNo: number, { hx, hz, h }: CarDims): ZoneSpec {
  const side = (s: 1 | -1): Pick<ZoneSpec, 'boxes' | 'paths'> => ({
    boxes: [[s * (hx + 0.012), h * 0.33, 0, 0.02, h * 0.42, hz * 1.75]],
    paths: [s > 0
      ? [[hx + 0.6, 0, -hz - 0.15], [hx + 0.7, 0, 0], [hx + 0.6, 0, hz + 0.15]]
      : [[-hx - 0.6, 0, hz + 0.15], [-hx - 0.7, 0, 0], [-hx - 0.6, 0, -hz - 0.15]]],
  });
  switch (partNo) {
    case 2: return { dir: [-1, 0.42, 0.32], ...side(-1) };
    case 3: return {
      dir: [0.75, 0.9, 0.55], pad: 0.5,
      boxes: [[0, h * 0.35, hz + 0.012, hx * 1.8, h * 0.45, 0.02], [0, h * 0.4, -hz - 0.012, hx * 1.8, h * 0.5, 0.02]],
      paths: [
        [[hx + 0.45, 0, hz - 0.1], [hx * 0.6, 0, hz + 0.5], [-hx * 0.6, 0, hz + 0.5], [-hx - 0.45, 0, hz - 0.1]],
        [[-hx - 0.45, 0, -hz + 0.1], [-hx * 0.6, 0, -hz - 0.5], [hx * 0.6, 0, -hz - 0.5], [hx + 0.45, 0, -hz + 0.1]],
      ],
    };
    case 4: return { dir: [1, 0.3, 0.55], wheels: true, boxes: [], paths: [[[hx + 0.45, 0, hz * 0.62], [hx + 0.55, 0, 0], [hx + 0.45, 0, -hz * 0.62]]] };
    case 5: return { dir: [0.55, 0.95, 0.9], onTop: true, boxes: [[0, h * 0.58, hz * 0.55, hx * 1.6, h * 0.12, hz * 0.22]], paths: [] };
    case 6: return {
      dir: [0.35, 0.7, 1], pad: 0.3, onTop: true,
      boxes: [[0, h * 0.42, hz * 0.86, hx * 1.7, h * 0.14, hz * 0.26]],
      paths: [[[hx * 0.8, 0, hz + 0.5], [0, 0, hz + 0.62], [-hx * 0.8, 0, hz + 0.5]]],
    };
    case 7: return {
      dir: [1, 0.14, -0.25], onTop: true,
      boxes: [[0, h * 0.09, 0, hx * 1.7, 0.03, hz * 1.7]],
      paths: [[[hx + 0.35, 0, hz * 0.8], [hx + 0.4, 0, 0], [hx + 0.35, 0, -hz * 0.8]]],
    };
    case 8: return { dir: [0.45, 1.1, -0.65], onTop: true, boxes: [[0, h * 0.5, 0, hx * 1.6, h * 0.42, hz * 1.4]], paths: [] };
    default: return { dir: [1, 0.42, -0.32], ...side(1) };
  }
}
