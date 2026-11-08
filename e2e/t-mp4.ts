import fs from 'node:fs';
import { buildRunMp4 } from './src/artifacts.js';
const d = '20261108T040739Z';
const sum = JSON.parse(fs.readFileSync(`out/${d}/summary.json`, 'utf8'));
const r = buildRunMp4([`out/${d}/video-1.webm`], sum.steps);
console.log(JSON.stringify(r));
