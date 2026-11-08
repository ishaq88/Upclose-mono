import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { HDRLoader } from 'three/addons/loaders/HDRLoader.js';
import { DecalGeometry } from 'three/addons/geometries/DecalGeometry.js';
import config from '../scene.json';
import damageInput from '../damage.json';
import { validateDamage, type FindingPin, type Vec3 } from './types';
import { damageTexture } from './textures';
import { GroundTruth, type Decal } from './ground-truth';
import './style.css';
import { guidedCameraAt, type RenderZone } from './zones';
import { instrumentCluster, INDICATORS, type DashVariant } from './cluster';

const damage = validateDamage(damageInput);
const captureMode = new URLSearchParams(location.search).has('capture');
if (new URLSearchParams(location.search).has('zones')) {
  config.width = 1280; config.height = 720;
}
document.body.classList.toggle('capture', captureMode);
const canvas = document.querySelector<HTMLCanvasElement>('#scene')!;
const status = document.querySelector<HTMLElement>('#status')!;
const scene = new THREE.Scene();
scene.background = new THREE.Color('#e7e9e6');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(captureMode ? 1 : Math.min(window.devicePixelRatio, 1.5));
renderer.setSize(captureMode ? config.width : innerWidth, captureMode ? config.height : innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 0.9;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
const camera = new THREE.PerspectiveCamera(config.fovDegrees, canvas.width / canvas.height, 0.1, 60);
camera.position.set(-5, 2.2, -5.8);
const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 0.65, 0);
controls.enableDamping = true;
controls.minDistance = 3; controls.maxDistance = 12;
controls.maxPolarAngle = Math.PI / 2 - 0.03;
controls.update();
const markers = new THREE.Group(); scene.add(markers);
const findingPins = new THREE.Group(); scene.add(findingPins);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.MeshStandardMaterial({ color: '#d0d5d0', roughness: 0.87 }));
ground.rotation.x = -Math.PI / 2; ground.position.y = -0.012; ground.receiveShadow = true; scene.add(ground);
const key = new THREE.DirectionalLight('#fff4df', 2.2);
key.position.set(-3, 7, -4); key.castShadow = true;
key.shadow.mapSize.set(2048, 2048); key.shadow.camera.left = -5; key.shadow.camera.right = 5;
key.shadow.camera.top = 5; key.shadow.camera.bottom = -5; key.shadow.normalBias = 0.02;
key.shadow.radius = 4; scene.add(key);
scene.add(new THREE.HemisphereLight('#e6eff5', '#929486', 0.45));

// Phase 2 entry point. These are display pins only, never inferred verdicts or scores.
export function addFindingPins(pins: readonly FindingPin[]): void {
  findingPins.children.forEach(child => {
    const mesh = child as THREE.Mesh; mesh.geometry.dispose(); (mesh.material as THREE.Material).dispose();
  });
  findingPins.clear();
  for (const pin of pins) {
    if (!pin.evidenceId || !pin.position.every(Number.isFinite)) throw new Error('Finding pins require an evidence ID and finite position');
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.045, 16, 12), new THREE.MeshBasicMaterial({ color: '#478cdd' }));
    mesh.position.fromArray(pin.position); mesh.userData = { pin }; findingPins.add(mesh);
  }
}

function cameraAt(frame: number, frameCount: number): void {
  camera.fov = config.fovDegrees; camera.updateProjectionMatrix();
  const phase = 2 * Math.PI * frame / frameCount;
  // Angular speed varies from 0.35x to 1.65x; four smooth pauses near panel views.
  const angle = -Math.PI / 2 + phase - 0.1625 * Math.sin(4 * phase);
  const radius = 5.5 + 0.18 * Math.sin(2 * phase);
  camera.position.set(Math.sin(angle) * radius, 1.2 + 0.018 * Math.sin(phase * 12), Math.cos(angle) * radius);
  camera.lookAt(0, 0.64, 0); camera.updateMatrixWorld(true);
}

function selectDamage(id: string): void {
  const item = damage.find(d => d.id === id)!;
  document.querySelector('#detail')!.textContent = `${item.type.toUpperCase()} · ${item.zone.replaceAll('_', ' ')}. Known placement in meters: ${item.position.join(', ')}. Synthetic ${item.type === 'dent' ? 'shading decal; not deformed metal.' : 'projected texture.'}`;
}

function outwardDecal(source: THREE.BufferGeometry, outward: THREE.Vector3): THREE.BufferGeometry {
  const position = source.getAttribute('position'), normal = source.getAttribute('normal'), uv = source.getAttribute('uv');
  const positions: number[] = [], normals: number[] = [], uvs: number[] = [];
  const average = new THREE.Vector3();
  for (let i = 0; i < position.count; i += 3) {
    average.set(0, 0, 0);
    for (let j = 0; j < 3; j++) average.add(new THREE.Vector3().fromBufferAttribute(normal, i + j));
    // A projector volume can intersect the inner skin of a thin body panel.
    // Never paint the back of that panel with exterior damage.
    if (average.dot(outward) <= 0) continue;
    for (let j = 0; j < 3; j++) {
      const k = i + j;
      positions.push(position.getX(k), position.getY(k), position.getZ(k));
      normals.push(normal.getX(k), normal.getY(k), normal.getZ(k));
      uvs.push(uv.getX(k), uv.getY(k));
    }
  }
  const result = new THREE.BufferGeometry();
  result.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  result.setAttribute('normal', new THREE.Float32BufferAttribute(normals, 3));
  result.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
  source.dispose();
  return result;
}

async function start(): Promise<void> {
  const draco = new DRACOLoader(); draco.setDecoderPath('/draco/');
  const [gltf, hdr] = await Promise.all([
    new GLTFLoader().setDRACOLoader(draco).loadAsync(config.model.url),
    new HDRLoader().loadAsync(config.environment),
  ]);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromEquirectangular(hdr).texture; hdr.dispose(); pmrem.dispose();
  const car = gltf.scene;
  car.rotation.y = config.model.rotationY; car.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(car);
  const size = bounds.getSize(new THREE.Vector3());
  car.scale.multiplyScalar(config.model.lengthMeters / Math.max(size.x, size.z)); car.updateMatrixWorld(true);
  bounds.setFromObject(car);
  car.position.add(new THREE.Vector3(-(bounds.min.x + bounds.max.x) / 2, -bounds.min.y, -(bounds.min.z + bounds.max.z) / 2));
  car.updateMatrixWorld(true); scene.add(car);
  const objects: THREE.Mesh[] = [ground];
  const cluster = instrumentCluster(); scene.add(cluster.group);
  cluster.group.traverse(object => { if (object instanceof THREE.Mesh) objects.push(object); });
  let body: THREE.Mesh | undefined;
  car.traverse(object => {
    if (!(object instanceof THREE.Mesh)) return;
    object.castShadow = true; object.receiveShadow = true;
    objects.push(object);
    // The FBX conversion predates calibrated PBR materials. Replace its pale
    // plastic/carpet and low-metalness chrome with plausible surface responses.
    const sourceMaterials = Array.isArray(object.material) ? object.material : [object.material];
    sourceMaterials.forEach(material => {
      if (!(material instanceof THREE.MeshStandardMaterial)) return;
      if (/Tires/i.test(material.name)) { material.color.set('#16191a'); material.metalness = 0; material.roughness = 0.9; }
      else if (/Leather|Interior|Carpet|Carbon|plastic/i.test(material.name)) { material.color.set('#24282a'); material.metalness = 0; material.roughness = 0.8; }
      else if (/metal/i.test(material.name)) { material.color.set('#969d9e'); material.metalness = 0.95; material.roughness = 0.25; }
      else if (/Taillight/i.test(material.name)) { material.color.set('#8e080c'); material.metalness = 0.2; material.roughness = 0.2; }
    });
    if (object.name === config.model.bodyMesh) {
      body = object;
      object.material = new THREE.MeshPhysicalMaterial({ color: config.model.paint, metalness: 0.8, roughness: 0.33, clearcoat: 0.7, clearcoatRoughness: 0.21 });
    }
    if (object.name === 'glass') object.material = new THREE.MeshPhysicalMaterial({ color: '#657b83', metalness: 0.15, roughness: 0.13, transparent: true, opacity: 0.65 });
  });
  if (!body) throw new Error(`Configured body mesh "${config.model.bodyMesh}" missing; update scene.json for the replacement model`);
  const decals: Decal[] = [];
  const placements: { id: string; surfacePosition: Vec3; vertexCount: number }[] = [];
  for (const [index, item] of damage.entries()) {
    const normal = new THREE.Vector3().fromArray(item.normal);
    const probe = new THREE.Raycaster(new THREE.Vector3().fromArray(item.position).addScaledVector(normal, 0.75), normal.clone().negate(), 0, 1.5);
    const hit = probe.intersectObject(body, false)[0];
    if (!hit) throw new Error(`Damage ${item.id} misses configured body mesh`);
    const orientation = new THREE.Euler().setFromQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal));
    const geometry = outwardDecal(new DecalGeometry(body, hit.point, orientation, new THREE.Vector3().fromArray(item.size)), normal);
    if (geometry.getAttribute('position').count === 0) throw new Error(`Empty decal: ${item.id}`);
    const texture = damageTexture(item.type);
    const material = new THREE.MeshStandardMaterial({ map: texture, transparent: true, depthWrite: false, alphaTest: 0.10, polygonOffset: true, polygonOffsetFactor: -4, roughness: 0.95, metalness: 0.03 });
    const mesh = new THREE.Mesh(geometry, material); mesh.renderOrder = index + 1; scene.add(mesh); objects.push(mesh);
    // The ID pass uses texture alpha only. RGB must remain the exact integer ID.
    const idMaterial = new THREE.ShaderMaterial({
      uniforms: { image: { value: texture }, id: { value: (index + 1) / 255 } },
      vertexShader: 'varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: 'uniform sampler2D image; uniform float id; varying vec2 vUv; void main(){if(texture2D(image,vUv).a<0.10)discard;gl_FragColor=vec4(id,0.,0.,1.);}',
      polygonOffset: true, polygonOffsetFactor: -4,
    });
    idMaterial.toneMapped = false;
    decals.push({ damage: item, mesh, idMaterial });
    placements.push({ id: item.id, surfacePosition: hit.point.toArray() as Vec3, vertexCount: geometry.getAttribute('position').count });
    const marker = new THREE.Mesh(new THREE.SphereGeometry(0.038, 16, 12), new THREE.MeshBasicMaterial({ color: '#d67c47' }));
    marker.position.copy(hit.point).addScaledVector(normal, 0.045); marker.userData = { damageId: item.id }; markers.add(marker);
    const button = document.createElement('button');
    button.innerHTML = `<i></i>${item.type[0].toUpperCase() + item.type.slice(1)}<small>${item.zone.replaceAll('_', ' ')}</small>`;
    button.addEventListener('click', () => selectDamage(item.id)); document.querySelector('#damage-list')!.append(button);
  }
  const truth = new GroundTruth(renderer, scene, camera, objects, decals, config.width, config.height);
  markers.visible = !captureMode;
  // Parallel shader compilation can otherwise omit a newly encountered decal
  // from the first ID pass on hardware drivers. Ready means both passes are warm.
  await truth.prepare();
  cluster.group.visible = true;
  await renderer.compileAsync(scene, camera);
  cluster.group.visible = false;
  document.querySelector('#count')!.textContent = String(damage.length).padStart(2, '0');
  status.textContent = 'Ready · synthetic car / known damage';
  let playing = false, started = 0;
  document.querySelector('#markers')!.addEventListener('change', event => { markers.visible = (event.target as HTMLInputElement).checked; });
  document.querySelector('#walkaround')!.addEventListener('click', () => {
    playing = !playing; started = performance.now(); controls.enabled = !playing;
    document.querySelector('#walkaround')!.textContent = playing ? 'Stop walkaround' : 'Preview walkaround ↗';
  });
  canvas.addEventListener('click', event => {
    const rect = canvas.getBoundingClientRect(), ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), camera);
    const hit = ray.intersectObjects([...markers.visible ? markers.children : [], ...findingPins.children])[0];
    if (hit?.object.userData.damageId) selectDamage(hit.object.userData.damageId);
    if (hit?.object.userData.pin) document.querySelector('#detail')!.textContent = `${hit.object.userData.pin.label} · evidence ${hit.object.userData.pin.evidenceId}`;
  });
  const api = {
    ready: true, config, damage, placements, addFindingPins,
    capture(frame: number, frameCount: number, zone?: RenderZone, dashVariant: DashVariant = 'warnings') {
      if (!captureMode) throw new Error('Use ?capture for deterministic output');
      if (!Number.isInteger(frame) || frame < 0 || frame >= frameCount) throw new Error('Frame outside render');
      markers.visible = false; findingPins.visible = false;
      cluster.group.visible = zone === 'dash_electrical'; cluster.setVariant(dashVariant);
      if (zone) guidedCameraAt(camera, zone, frame / Math.max(1, frameCount - 1));
      else cameraAt(frame, frameCount);
      const visibleDamage = truth.capture();
      renderer.render(scene, camera);
      const fy = config.height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
      return { frame, timestamp_seconds: frame / config.fps, camera: {
        position_m: camera.position.toArray(), quaternion_xyzw: camera.quaternion.toArray(),
        camera_to_world_column_major: camera.matrixWorld.toArray(),
        world_to_camera_column_major: camera.matrixWorldInverse.toArray(),
        intrinsics: { fx: fy, fy, cx: config.width / 2, cy: config.height / 2, width: config.width, height: config.height },
      }, visible_damage: visibleDamage,
      ...(zone === 'dash_electrical' ? { lit_indicators: dashVariant === 'warnings' ? [...INDICATORS] : [], tachometer_rpm: 1800 } : {}) };
    },
  };
  Object.assign(window, { twin: api });
  if (captureMode) cameraAt(0, config.durationSeconds * config.fps);
  let lastDraw = 0;
  renderer.setAnimationLoop(captureMode ? null : (now: number) => {
    if (now - lastDraw < 33) return;
    lastDraw = now;
    if (playing) cameraAt(((performance.now() - started) / 1000 * config.fps) % (config.durationSeconds * config.fps), config.durationSeconds * config.fps);
    else controls.update();
    renderer.render(scene, camera);
  });
  renderer.render(scene, camera);
  window.addEventListener('resize', () => {
    if (captureMode) return;
    camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight);
  });
}
start().catch(error => { status.textContent = `Scene failed: ${String(error)}`; Object.assign(window, { twinError: String(error) }); console.error(error); });
