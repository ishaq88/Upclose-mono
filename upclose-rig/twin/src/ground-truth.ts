import * as THREE from 'three';
import type { Damage, VisibleDamage } from './types';

export interface Decal { damage: Damage; mesh: THREE.Mesh; idMaterial: THREE.ShaderMaterial }

/** Full-resolution ID pass. The real car writes depth; decal alpha and depth are tested per pixel. */
export class GroundTruth {
  private target: THREE.WebGLRenderTarget;
  private pixels: Uint8Array;
  private black = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
  constructor(private renderer: THREE.WebGLRenderer, private scene: THREE.Scene, private camera: THREE.PerspectiveCamera,
    private objects: THREE.Mesh[], private decals: Decal[], private width: number, private height: number) {
    this.target = new THREE.WebGLRenderTarget(width, height, { minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, samples: 0 });
    this.target.texture.colorSpace = THREE.NoColorSpace;
    this.pixels = new Uint8Array(width * height * 4);
    this.black.toneMapped = false;
  }
  async prepare(): Promise<void> {
    const restore = this.useIdMaterials();
    try { await this.renderer.compileAsync(this.scene, this.camera); }
    finally { restore(); }
  }
  private useIdMaterials(): () => void {
    const materials = this.objects.map(mesh => mesh.material);
    this.objects.forEach(mesh => { mesh.material = this.black; });
    this.decals.forEach(decal => { decal.mesh.material = decal.idMaterial; });
    return () => { this.objects.forEach((mesh, i) => { mesh.material = materials[i]; }); };
  }
  capture(): VisibleDamage[] {
    const restore = this.useIdMaterials();
    const background = this.scene.background;
    const toneMapping = this.renderer.toneMapping;
    const target = this.renderer.getRenderTarget();
    const shadowAutoUpdate = this.renderer.shadowMap.autoUpdate;
    try {
      this.renderer.shadowMap.autoUpdate = false;
      this.scene.background = new THREE.Color(0);
      this.renderer.toneMapping = THREE.NoToneMapping;
      this.renderer.setRenderTarget(this.target);
      this.renderer.render(this.scene, this.camera);
      this.renderer.readRenderTargetPixels(this.target, 0, 0, this.width, this.height, this.pixels);
    } finally {
      restore();
      this.scene.background = background;
      this.renderer.toneMapping = toneMapping;
      this.renderer.shadowMap.autoUpdate = shadowAutoUpdate;
      this.renderer.setRenderTarget(target);
    }
    const boxes = this.decals.map(() => ({ x0: this.width, y0: this.height, x1: 0, y1: 0, count: 0 }));
    for (let i = 0; i < this.width * this.height; i++) {
      const id = this.pixels[i * 4];
      if (id < 1 || id > boxes.length || this.pixels[i * 4 + 1] !== 0 || this.pixels[i * 4 + 2] !== 0) continue;
      const x = i % this.width, y = this.height - 1 - Math.floor(i / this.width), box = boxes[id - 1];
      box.x0 = Math.min(box.x0, x); box.x1 = Math.max(box.x1, x + 1);
      box.y0 = Math.min(box.y0, y); box.y1 = Math.max(box.y1, y + 1); box.count++;
    }
    return boxes.flatMap((box, i) => box.count === 0 ? [] : [{
      id: this.decals[i].damage.id, type: this.decals[i].damage.type, zone: this.decals[i].damage.zone,
      bbox_xyxy: [box.x0, box.y0, box.x1, box.y1] as [number, number, number, number], visible_pixels: box.count,
    }]);
  }
}
