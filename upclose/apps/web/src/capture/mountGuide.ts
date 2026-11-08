// 3D capture guide: the van with the current zone highlighted, a walking path and a camera that glides to each zone.
// Loaded lazily (see Guide3D.tsx). Model: Kenney Car Kit "van" (CC0, /models/KENNEY-LICENSE.txt).
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { zoneSpec, type CarDims } from './zones';

const ORANGE = new THREE.Color('#ff5a1f');
const DONE = new THREE.Color('#1f6f8b');

export interface GuideHandle {
  setZone: (partNo: number) => void;
  /** Zones already filmed: drawn as a quiet green wash on the car so progress reads at a glance. */
  setDone: (partNos: readonly number[]) => void;
  dispose: () => void;
}

interface LoadedCar { car: THREE.Object3D; dims: CarDims }
let modelPromise: Promise<LoadedCar> | null = null;

function loadCar(): Promise<LoadedCar> {
  modelPromise ??= new GLTFLoader().loadAsync('/models/van.glb').then((gltf) => {
    const car = gltf.scene;
    car.traverse((o) => {
      if (!(o instanceof THREE.Mesh)) return;
      const mat = new THREE.MeshStandardMaterial({ map: (o.material as THREE.MeshStandardMaterial).map, roughness: 0.42, metalness: 0.2, envMapIntensity: 0.9 });
      // Studio "clay" look: keep the atlas' light and dark values, drop its toy colours.
      mat.onBeforeCompile = (s) => {
        s.fragmentShader = s.fragmentShader.replace('#include <map_fragment>', `
          vec4 texelColor = texture2D( map, vMapUv );
          float g = dot( texelColor.rgb, vec3( 0.299, 0.587, 0.114 ) );
          diffuseColor.rgb *= mix( vec3( 0.13, 0.14, 0.16 ), vec3( 0.95, 0.955, 0.96 ), smoothstep( 0.1, 0.7, g ) );`);
      };
      o.material = mat;
      o.castShadow = true;
    });
    const box = new THREE.Box3().setFromObject(car);
    const centre = box.getCenter(new THREE.Vector3());
    car.position.set(-centre.x, -box.min.y, -centre.z);
    const size = box.getSize(new THREE.Vector3());
    return { car, dims: { hx: size.x / 2, hz: size.z / 2, h: size.y } };
  });
  // A failed load must be retryable on the next mount.
  modelPromise.catch(() => { modelPromise = null; });
  return modelPromise.then(({ car, dims }) => ({ car: car.clone(true), dims }));
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** Mounts the guide into `el`. Rejects when WebGL or the model is unavailable; the caller shows the text steps. */
export async function mountGuide(el: HTMLElement, initialPartNo: number): Promise<GuideHandle> {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); // throws without WebGL
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.style.cssText = 'display:block;width:100%;height:100%';
  el.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envTarget = pmrem.fromScene(new RoomEnvironment(), 0.04);
  scene.environment = envTarget.texture;
  scene.add(new THREE.HemisphereLight(0xffffff, 0xcfd3d8, 0.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(3, 6, 2);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -4, right: 4, top: 4, bottom: -4 });
  sun.shadow.radius = 8;
  scene.add(sun);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(20, 20), new THREE.ShadowMaterial({ opacity: 0.14 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 60);
  const controls = new OrbitControls(camera, renderer.domElement);
  Object.assign(controls, { enablePan: false, enableZoom: false, enableDamping: true, maxPolarAngle: Math.PI / 2.05, minPolarAngle: 0.15 });

  let disposed = false;
  let observer: ResizeObserver | null = null;
  const fx = new THREE.Group();
  const doneFx = new THREE.Group();
  const pulse: THREE.MeshBasicMaterial[] = [];
  const doneDisposables: { dispose: () => void }[] = [];
  const fxDisposables: { dispose: () => void }[] = [];
  const clearFx = (): void => {
    fx.clear();
    pulse.length = 0;
    for (const item of fxDisposables) item.dispose();
    fxDisposables.length = 0;
  };
  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    renderer.setAnimationLoop(null);
    observer?.disconnect();
    controls.dispose();
    clearFx();
    for (const item of doneDisposables) item.dispose();
    ground.geometry.dispose();
    (ground.material as THREE.Material).dispose();
    envTarget.dispose();
    pmrem.dispose();
    renderer.dispose();
    renderer.domElement.remove();
  };

  let loaded: LoadedCar;
  try {
    loaded = await loadCar();
  } catch (error) {
    dispose();
    throw error;
  }
  if (disposed) throw new Error('Guide unmounted before it finished loading.');
  const { car, dims } = loaded;
  scene.add(car);
  scene.add(fx);
  scene.add(doneFx);

  const reduceMotion = prefersReducedMotion();
  const wheels: THREE.Mesh[] = [];
  car.traverse((o) => { if (o instanceof THREE.Mesh && (o.name + (o.parent?.name ?? '')).includes('wheel')) wheels.push(o); });
  const wheelMats = wheels.map((w) => w.material);
  const tint = new THREE.MeshStandardMaterial({ color: ORANGE, roughness: 0.5 });
  const radius = Math.hypot(dims.hx + 0.45, dims.h / 2, dims.hz + 0.3);
  const target = new THREE.Vector3(0, dims.h * 0.4, 0);
  let from = { cam: new THREE.Vector3(), at: new THREE.Vector3() };
  let to = { cam: new THREE.Vector3(), at: new THREE.Vector3() };
  let tween = 1;

  const distance = (pad = 0): number => {
    const v = THREE.MathUtils.degToRad(camera.fov) / 2;
    const hfov = Math.atan(Math.tan(v) * camera.aspect);
    return ((radius + pad) / Math.sin(Math.min(v, hfov))) * 0.74;
  };
  const track = <T extends { dispose: () => void }>(item: T): T => { fxDisposables.push(item); return item; };

  let activePart = initialPartNo;
  let donePartsList: readonly number[] = [];
  const drawDone = (): void => {
    doneFx.clear();
    for (const item of doneDisposables) item.dispose();
    doneDisposables.length = 0;
    for (const partNo of donePartsList) {
      if (partNo === activePart) continue;
      const zone = zoneSpec(partNo, dims);
      for (const [x, y, zz, w, hh, d] of zone.boxes) {
        const geo = new THREE.BoxGeometry(w, hh, d);
        doneDisposables.push(geo);
        let mesh: THREE.Object3D;
        if (zone.onTop) {
          const edges = new THREE.EdgesGeometry(geo);
          const mat = new THREE.LineBasicMaterial({ color: DONE, depthTest: false, transparent: true, opacity: 0.8 });
          doneDisposables.push(edges, mat);
          mesh = new THREE.LineSegments(edges, mat);
        } else {
          const mat = new THREE.MeshBasicMaterial({ color: DONE, transparent: true, opacity: 0.3, depthWrite: false });
          doneDisposables.push(mat);
          mesh = new THREE.Mesh(geo, mat);
        }
        mesh.position.set(x, y, zz);
        mesh.renderOrder = 1;
        doneFx.add(mesh);
      }
    }
  };
  const setDone = (partNos: readonly number[]): void => {
    const same = partNos.length === donePartsList.length && partNos.every((n, i) => n === donePartsList[i]);
    if (same) return;
    donePartsList = [...partNos];
    drawDone();
  };

  const setZone = (partNo: number): void => {
    activePart = partNo;
    drawDone();
    const z = zoneSpec(partNo, dims);
    clearFx();
    for (const [x, y, zz, w, hh, d] of z.boxes) {
      const geo = track(new THREE.BoxGeometry(w, hh, d));
      let mesh: THREE.Object3D;
      if (z.onTop) {
        const edges = track(new THREE.EdgesGeometry(geo));
        mesh = new THREE.LineSegments(edges, track(new THREE.LineBasicMaterial({ color: ORANGE, depthTest: false, transparent: true })));
      } else {
        const glow = track(new THREE.MeshBasicMaterial({ color: ORANGE, transparent: true, opacity: 0.45, depthWrite: false }));
        pulse.push(glow);
        mesh = new THREE.Mesh(geo, glow);
      }
      mesh.position.set(x, y, zz);
      mesh.renderOrder = 2;
      fx.add(mesh);
    }
    wheels.forEach((w, i) => { w.material = z.wheels ? tint : wheelMats[i]!; });
    for (const path of z.paths) {
      const curve = new THREE.CatmullRomCurve3(path.map((p) => new THREE.Vector3(p[0], 0.015, p[2])));
      const lineGeo = track(new THREE.BufferGeometry().setFromPoints(curve.getPoints(90)));
      const dash = new THREE.Line(lineGeo, track(new THREE.LineDashedMaterial({ color: ORANGE, dashSize: 0.12, gapSize: 0.08 })));
      dash.computeLineDistances();
      fx.add(dash);
      fx.add(new THREE.Mesh(track(new THREE.TubeGeometry(curve, 90, 0.022, 6, false)), track(new THREE.MeshBasicMaterial({ color: ORANGE, transparent: true, opacity: 0.85 }))));
      const end = curve.getPoint(1);
      const before = curve.getPoint(0.94);
      const arrow = new THREE.Mesh(track(new THREE.ConeGeometry(0.09, 0.22, 20)), track(new THREE.MeshBasicMaterial({ color: ORANGE })));
      arrow.position.copy(end);
      arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), end.clone().sub(before).normalize());
      fx.add(arrow);
      const startDot = new THREE.Mesh(track(new THREE.CircleGeometry(0.1, 32)), track(new THREE.MeshBasicMaterial({ color: 0x111315 })));
      startDot.rotation.x = -Math.PI / 2;
      startDot.position.copy(curve.getPoint(0)).setY(0.02);
      fx.add(startDot);
    }
    camera.aspect = el.clientWidth / Math.max(1, el.clientHeight);
    const dir = new THREE.Vector3(...z.dir).normalize();
    from = { cam: camera.position.clone(), at: controls.target.clone() };
    to = { cam: target.clone().add(dir.multiplyScalar(distance(z.pad))), at: target.clone() };
    tween = reduceMotion || from.cam.lengthSq() === 0 ? 1 : 0;
    if (tween === 1) { camera.position.copy(to.cam); controls.target.copy(to.at); }
  };

  const ease = (x: number): number => 1 - Math.pow(1 - x, 3);
  renderer.setAnimationLoop((time) => {
    if (!reduceMotion) for (const mat of pulse) mat.opacity = 0.4 + 0.16 * Math.sin(time / 380);
    if (tween < 1) {
      tween = Math.min(1, tween + 0.03);
      camera.position.lerpVectors(from.cam, to.cam, ease(tween));
      controls.target.lerpVectors(from.at, to.at, ease(tween));
    }
    controls.update();
    renderer.render(scene, camera);
  });
  const fit = (): void => {
    renderer.setSize(el.clientWidth, el.clientHeight, false);
    camera.aspect = el.clientWidth / Math.max(1, el.clientHeight);
    camera.updateProjectionMatrix();
  };
  observer = typeof ResizeObserver === 'function' ? new ResizeObserver(fit) : null;
  observer?.observe(el);
  fit();
  setZone(initialPartNo);
  return { setZone, setDone, dispose };
}
