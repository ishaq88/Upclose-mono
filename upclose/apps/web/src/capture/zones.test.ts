import { describe, expect, it } from 'vitest';
import { GUIDE_STEPS } from './guide';
import { DISPLAY_ORDER, OPTIONAL_PARTS, sortForDisplay, walkText, zoneSpec } from './zones';

const dims = { hx: 1, hz: 2.2, h: 1.8 };

describe('capture zones', () => {
  it('shows the optional zones last without changing part numbers', () => {
    const parts = GUIDE_STEPS.map((step) => ({ part_no: step.partNo }));
    expect(sortForDisplay(parts).map((part) => part.part_no)).toEqual([1, 2, 3, 4, 6, 7, 5, 8]);
    expect([...DISPLAY_ORDER].sort()).toEqual(GUIDE_STEPS.map((step) => step.partNo));
    expect([...OPTIONAL_PARTS].sort()).toEqual([5, 8]);
  });

  it('gives every zone a walk line and something to highlight', () => {
    for (const step of GUIDE_STEPS) {
      const spec = zoneSpec(step.partNo, dims);
      expect(walkText(step.partNo).length).toBeGreaterThan(5);
      expect(spec.boxes.length + spec.paths.length + (spec.wheels ? 1 : 0)).toBeGreaterThan(0);
    }
  });

  it('never tells anyone to go under the car on the underbody zone', () => {
    const text = `${walkText(7)} ${GUIDE_STEPS.find((step) => step.partNo === 7)!.steps.join(' ')}`;
    expect(text).not.toMatch(/crouch|crawl under|lie under|go under(?! the car)/i);
    expect(walkText(7)).toMatch(/standing/i);
  });
});
