import { describe, expect, it } from 'vitest';
import { parseVideoRanges } from './videoRanges';

describe('optional source declarations', () => {
  it('accepts no hints and partial non-overlapping hints with millisecond timestamps', () => {
    expect(parseVideoRanges([], null)).toEqual([]);
    expect(parseVideoRanges([{ partNo: 2, start: '10.125', end: '30' }], 45)).toEqual([{ part_no: 2, start_ms: 10125, end_ms: 30000 }]);
  });
  it.each([
    [{ partNo: 1, start: '', end: '10' }],
    [{ partNo: 1, start: '0', end: '61' }],
    [{ partNo: 1, start: '30', end: '20' }],
    [{ partNo: 1, start: '160', end: '181' }],
    [{ partNo: 1, start: '0', end: '10' }, { partNo: 1, start: '10', end: '20' }],
    [{ partNo: 1, start: '0', end: '20' }, { partNo: 2, start: '10', end: '30' }],
  ])('rejects invalid range declarations %j', (...items) => {
    expect(() => parseVideoRanges(items, null)).toThrow();
  });
  it('rejects a range beyond the measured source duration', () => {
    expect(() => parseVideoRanges([{ partNo: 1, start: '0', end: '30' }], 20)).toThrow(/video ends/);
  });
});
