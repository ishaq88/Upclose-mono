import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

const directory = process.argv[2];
if (!directory) throw new Error('Usage: node scripts/audit.mjs out/<render-id> (run from twin root)');
const truth = JSON.parse(await readFile(path.join(directory, 'ground_truth.json'), 'utf8'));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
assert.equal(truth.provenance, 'synthetic');
assert.equal(truth.frame_count, truth.frames.length);
assert.equal(truth.frame_count, truth.duration_seconds * truth.fps);
assert.equal(truth.video_sha256, hash(await readFile(path.join(directory, truth.video))));
for (const [file, expected] of Object.entries(truth.input_sha256)) assert.equal(hash(await readFile(file)), expected, `Changed input: ${file}`);
const seen = new Set();
for (const [index, frame] of truth.frames.entries()) {
  assert.equal(frame.frame, index); assert.equal(frame.timestamp_seconds, index / truth.fps);
  assert.equal(frame.camera.camera_to_world_column_major.length, 16);
  assert.equal(frame.camera.world_to_camera_column_major.length, 16);
  assert([...frame.camera.camera_to_world_column_major, ...frame.camera.world_to_camera_column_major, ...frame.camera.position_m, ...frame.camera.quaternion_xyzw].every(Number.isFinite));
  const calibration = frame.camera.intrinsics;
  assert.equal(calibration.width, truth.width); assert.equal(calibration.height, truth.height);
  assert(calibration.fx > 0 && calibration.fy > 0);
  for (const item of frame.visible_damage) {
    const damage = truth.damage.find(d => d.id === item.id);
    assert(damage && damage.zone === item.zone && damage.type === item.type);
    const [x0, y0, x1, y1] = item.bbox_xyxy;
    assert(item.bbox_xyxy.every(Number.isInteger));
    assert(x0 >= 0 && y0 >= 0 && x1 <= truth.width && y1 <= truth.height && x1 > x0 && y1 > y0);
    assert(item.visible_pixels > 0 && item.visible_pixels <= (x1 - x0) * (y1 - y0));
    seen.add(item.id);
  }
}
assert.equal(seen.size, truth.damage.length);
const result = { passed: true, render_id: truth.render_id, frame_count: truth.frames.length, video_sha256_verified: true, current_input_hashes_verified: true, timestamps_poses_intrinsics_and_boxes_verified: true, damage_items_visible: seen.size };
await writeFile(path.join(directory, 'check', 'metadata-audit.json'), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
