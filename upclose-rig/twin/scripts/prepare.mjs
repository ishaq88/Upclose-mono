import { cp, mkdir } from 'node:fs/promises';
await mkdir('public/draco', { recursive: true });
await cp('node_modules/three/examples/jsm/libs/draco/gltf', 'public/draco', { recursive: true });
await cp('node_modules/three/examples/jsm/libs/draco/README.md', 'public/draco/README.md');
await cp('public/assets/draco-LICENSE.txt', 'public/draco/LICENSE.txt');
console.log('Local Draco decoder ready (three.js bundled Apache-2.0 decoder).');
