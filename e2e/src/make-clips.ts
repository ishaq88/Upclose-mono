import { loadClips } from './clips.js';

const set = loadClips();
console.log(JSON.stringify({ source: set.source, dir: set.dir, zones: set.zones.map((z) => ({ zone: z.zone, filmed: z.file !== null, reason: z.reason })) }, null, 2));
