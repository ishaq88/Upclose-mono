import * as THREE from 'three';

export type RenderZone = 'driver_side' | 'passenger_side' | 'front_rear_body' | 'tires' | 'dash_electrical' | 'interior';

/** Continuous exterior paths and parked cockpit views; positions are meters. */
export function guidedCameraAt(camera: THREE.PerspectiveCamera, zone: RenderZone, t: number): void {
  const bob = 0.012 * Math.sin(t * Math.PI * 16);
  const sway = 0.018 * Math.sin(t * Math.PI * 10);
  camera.fov = zone === 'dash_electrical' || zone === 'interior' ? 65 : 55;
  if (zone === 'driver_side' || zone === 'passenger_side') {
    const driver = zone === 'driver_side';
    const z = driver ? 2 - 4 * t : -2 + 4 * t;
    camera.position.set((driver ? -1 : 1) * (2.65 + sway), 1.05 + bob, z);
    camera.lookAt(0, 0.55 + 0.09 * Math.cos(t * Math.PI * 4), z * 0.75);
  } else if (zone === 'front_rear_body') {
    // Front dwell, slow continuous walk along the passenger side, then rear dwell.
    const u = THREE.MathUtils.smoothstep(t, 0.15, 0.85);
    const angle = Math.PI - Math.PI * u;
    camera.position.set(Math.sin(angle) * 4.1 + sway, 1.3 + bob, Math.cos(angle) * 4.1);
    camera.lookAt(0, 0.65, 0);
  } else if (zone === 'tires') {
    // Slow at the four wheel corners; transition around the outside of the car.
    const p = t * Math.PI * 2;
    const angle = -Math.PI * 0.75 + p - 0.16 * Math.sin(4 * p);
    camera.position.set(Math.sin(angle) * 2.65, 0.48 + bob, Math.cos(angle) * 3.35);
    camera.lookAt(Math.sin(angle) * 0.85, 0.33, Math.cos(angle) * 1.5);
  } else if (zone === 'dash_electrical') {
    camera.fov = 40;
    camera.position.set(-0.36 + sway * 0.08, 1.01 + bob * 0.08, -0.02);
    camera.lookAt(-0.36 + sway * 0.025, 0.87 + bob * 0.025, -0.60);
  } else {
    camera.position.set(-0.62 + sway * 0.2, 1.05 + bob * 0.2, -0.15);
    camera.lookAt(0.35, 0.55 + 0.25 * t, 0.4 + 0.35 * t);
  }
  camera.updateProjectionMatrix(); camera.updateMatrixWorld(true);
}
