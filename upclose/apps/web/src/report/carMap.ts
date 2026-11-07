// Top-down 3D map of the vehicle with finding pins. Loaded lazily; three.js never ships with the landing page.
// Model: Kenney Car Kit "van" (CC0, /models/KENNEY-LICENSE.txt). Car space after centring: front = +z, driver side = +x.
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

export type SideState = 'assessed' | 'partial' | 'not_assessed';
export interface MapPin { n: number; id: string; label: string; serious: boolean; u: number; v: number }
export interface MapInput { sides: { driver: SideState; passenger: SideState; front: SideState; rear: SideState }; pins: MapPin[]; onPin?: (id: string) => void }

/** Where each component sits: u across (-1 passenger … +1 driver), v along (-1 rear … +1 front). */
export const COMPONENT_POSITION: Record<string, [number, number]> = {
  driver_rocker: [1, -0.45], passenger_rocker: [-1, -0.45],
  driver_front_door: [1, 0.4], driver_rear_door: [1, -0.1], passenger_front_door: [-1, 0.4], passenger_rear_door: [-1, -0.1],
  tire_fl: [1, 0.6], tire_fr: [-1, 0.6], tire_rl: [1, -0.6], tire_rr: [-1, -0.6],
  front_bumper: [0.55, 1.1], rear_bumper: [0, -1.1], hood: [0, 0.8], windshield: [0, 0.55], roof: [0, 0], trunk: [0, -0.9],
};

function hatchTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d')!;
  g.fillStyle = '#f1f3f5'; g.fillRect(0, 0, 64, 64);
  g.strokeStyle = '#b9c0c7'; g.lineWidth = 6;
  for (let i = -64; i < 128; i += 16) { g.beginPath(); g.moveTo(i, 64); g.lineTo(i + 64, 0); g.stroke(); }
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/** Mounts the map into `el`; returns a disposer. */
export async function mountMap(el: HTMLElement, input: MapInput): Promise<() => void> {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.domElement.style.cssText = 'display:block;width:100%;height:100%';
  el.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;
  scene.add(new THREE.HemisphereLight(0xffffff, 0xcfd3d8, 0.7));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4);
  sun.position.set(3, 6, 2);
  scene.add(sun);

  const gltf = await new GLTFLoader().loadAsync('/models/van.glb');
  const car = gltf.scene;
  car.traverse((o) => {
    if (!(o instanceof THREE.Mesh)) return;
    const mat = new THREE.MeshStandardMaterial({ map: (o.material as THREE.MeshStandardMaterial).map, roughness: 0.42, metalness: 0.2 });
    // Studio "clay" look: keep the atlas' light and dark values, drop its toy colours.
    mat.onBeforeCompile = (s) => {
      s.fragmentShader = s.fragmentShader.replace('#include <map_fragment>', `
        vec4 texelColor = texture2D( map, vMapUv );
        float g = dot( texelColor.rgb, vec3( 0.299, 0.587, 0.114 ) );
        diffuseColor.rgb *= mix( vec3( 0.13, 0.14, 0.16 ), vec3( 0.95, 0.955, 0.96 ), smoothstep( 0.1, 0.7, g ) );`);
    };
    o.material = mat;
  });
  const box = new THREE.Box3().setFromObject(car);
  const centre = box.getCenter(new THREE.Vector3());
  car.position.set(-centre.x, -box.min.y, -centre.z);
  const size = box.getSize(new THREE.Vector3());
  const hx = size.x / 2, hz = size.z / 2;
  scene.add(car);

  const hatch = hatchTexture();
  const strip = (x: number, z: number, w: number, d: number, state: SideState): void => {
    let mat: THREE.Material;
    if (state === 'not_assessed') {
      const t = hatch.clone(); t.repeat.set(w * 6, d * 6); t.needsUpdate = true;
      mat = new THREE.MeshBasicMaterial({ map: t });
    } else mat = new THREE.MeshBasicMaterial({ color: state === 'partial' ? '#f6d7a6' : '#d9dee3' });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), mat);
    m.rotation.x = -Math.PI / 2;
    m.position.set(x, 0.01, z);
    scene.add(m);
  };
  const gap = 0.08, t = 0.26;
  strip(hx + gap + t / 2, 0, t, hz * 2, input.sides.driver);
  strip(-hx - gap - t / 2, 0, t, hz * 2, input.sides.passenger);
  strip(0, hz + gap + t / 2, hx * 2, t, input.sides.front);
  strip(0, -hz - gap - t / 2, hx * 2, t, input.sides.rear);

  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
  camera.position.set(0, 12, 0);
  camera.up.set(0, 0, 1);
  camera.lookAt(0, 0, 0);

  const layer = document.createElement('div');
  layer.className = 'map-pins';
  el.appendChild(layer);
  const nodes = input.pins.map((p) => {
    const a = document.createElement('a');
    a.href = `#${p.id}`;
    a.className = p.serious ? 'map-pin map-pin-serious' : 'map-pin';
    a.textContent = String(p.n);
    a.setAttribute('aria-label', `Finding ${p.n}: ${p.label}`);
    a.addEventListener('click', () => input.onPin?.(p.id));
    layer.appendChild(a);
    return { a, at: new THREE.Vector3(p.u * (hx + gap + t / 2), 0, p.v * hz) };
  });

  const place = (): void => {
    const w = el.clientWidth, h = el.clientHeight, half = hz + gap + t + 0.2, aspect = w / Math.max(1, h);
    renderer.setSize(w, h, false);
    Object.assign(camera, { left: -half * aspect, right: half * aspect, top: half, bottom: -half });
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
    for (const { a, at } of nodes) {
      const p = at.clone().project(camera);
      a.style.left = `${((p.x + 1) / 2) * w}px`;
      a.style.top = `${((1 - p.y) / 2) * h}px`;
    }
  };
  const observer = new ResizeObserver(place);
  observer.observe(el);
  place();
  return () => { observer.disconnect(); renderer.dispose(); pmrem.dispose(); layer.remove(); renderer.domElement.remove(); };
}
