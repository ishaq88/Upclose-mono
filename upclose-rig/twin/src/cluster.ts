import * as THREE from 'three';

export const INDICATORS = ['check_engine', 'oil_pressure', 'tpms'] as const;
export type DashVariant = 'warnings' | 'clean';

/** Original canvas drawings, mounted as emissive decals on a synthetic cluster. */
export function instrumentCluster() {
  const group = new THREE.Group();
  group.position.set(-0.36, 0.87, -0.60);
  group.rotation.x = -0.22;
  const housing = new THREE.Mesh(new THREE.BoxGeometry(0.68, 0.30, 0.045),
    new THREE.MeshStandardMaterial({ color: '#111719', roughness: 0.75 }));
  group.add(housing);
  const dial = document.createElement('canvas'); dial.width = 2048; dial.height = 800;
  const ctx = dial.getContext('2d')!;
  ctx.fillStyle = '#080e14'; ctx.fillRect(0, 0, dial.width, dial.height);
  ctx.strokeStyle = '#263c49'; ctx.lineWidth = 8; ctx.strokeRect(12, 12, 2024, 776);
  drawGauge(480, 'RPM × 1000', 8, 1.8);
  drawGauge(1570, 'km/h', 240, 0);
  ctx.textAlign = 'center'; ctx.fillStyle = '#dae9ed'; ctx.font = '36px Arial';
  ctx.fillText('P', 1024, 205); ctx.font = 'bold 92px Arial'; ctx.fillText('0', 1024, 325);
  ctx.font = '28px Arial'; ctx.fillStyle = '#91a5ae'; ctx.fillText('km/h', 1024, 373);
  ctx.font = '22px Arial'; ctx.fillText('SYNTHETIC INSTRUMENTS', 1024, 745);
  const panel = new THREE.Mesh(new THREE.PlaneGeometry(0.65, 0.254),
    new THREE.MeshBasicMaterial({ map: texture(dial), toneMapped: false }));
  panel.position.z = 0.023; group.add(panel);

  const lamps = INDICATORS.map((id, i) => {
    const icon = document.createElement('canvas'); icon.width = 160; icon.height = 128;
    const c = icon.getContext('2d')!;
    c.strokeStyle = '#ffffff'; c.fillStyle = '#ffffff'; c.lineWidth = 8; c.lineJoin = 'round'; c.lineCap = 'round';
    if (id === 'check_engine') {
      // Engine block, cylinder head, intake and exhaust, drawn from primitives.
      c.beginPath(); c.moveTo(32, 42); c.lineTo(50, 42); c.lineTo(50, 28); c.lineTo(92, 28);
      c.lineTo(92, 42); c.lineTo(112, 42); c.lineTo(128, 57); c.lineTo(140, 57);
      c.lineTo(140, 92); c.lineTo(122, 92); c.lineTo(110, 104); c.lineTo(44, 104);
      c.lineTo(32, 90); c.closePath(); c.stroke();
      c.beginPath(); c.moveTo(20, 50); c.lineTo(20, 88); c.moveTo(20, 69); c.lineTo(32, 69); c.stroke();
    } else if (id === 'oil_pressure') {
      // Oil can, angled spout, handle and falling drop.
      c.beginPath(); c.moveTo(36, 54); c.lineTo(88, 54); c.lineTo(114, 38);
      c.lineTo(132, 44); c.lineTo(98, 76); c.lineTo(90, 96); c.lineTo(36, 96); c.closePath(); c.stroke();
      c.beginPath(); c.moveTo(40, 54); c.lineTo(40, 37); c.lineTo(65, 37); c.lineTo(65, 54);
      c.moveTo(36, 64); c.lineTo(17, 64); c.lineTo(17, 87); c.lineTo(36, 87); c.stroke();
      c.beginPath(); c.moveTo(132, 65); c.bezierCurveTo(117, 86, 116, 98, 132, 103);
      c.bezierCurveTo(149, 98, 148, 86, 132, 65); c.fill();
    } else {
      // Open tire cross-section with tread and central exclamation mark.
      c.beginPath(); c.moveTo(43, 22); c.bezierCurveTo(10, 55, 14, 98, 35, 106);
      c.lineTo(125, 106); c.bezierCurveTo(146, 98, 150, 55, 117, 22); c.stroke();
      for (let x = 35; x <= 125; x += 15) { c.beginPath(); c.moveTo(x, 106); c.lineTo(x, 116); c.stroke(); }
      c.fillRect(75, 34, 10, 40); c.beginPath(); c.arc(80, 89, 6, 0, Math.PI * 2); c.fill();
    }
    const iconTexture = texture(icon);
    const material = new THREE.MeshStandardMaterial({ color: '#000000', emissive: id === 'oil_pressure' ? '#ff2820' : '#ffb000',
      emissiveMap: iconTexture, map: iconTexture, emissiveIntensity: 1.8, transparent: true, alphaTest: 0.1, toneMapped: false });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(0.080, 0.064), material);
    mesh.position.set((i - 1) * 0.14, -0.073, 0.025); group.add(mesh);
    return mesh;
  });
  group.visible = false;
  return { group, lamps, setVariant(variant: DashVariant) { lamps.forEach(mesh => { mesh.visible = variant === 'warnings'; }); } };

  function drawGauge(x: number, title: string, maximum: number, value: number) {
    const y = 300, radius = 215;
    ctx.strokeStyle = '#446270'; ctx.lineWidth = 10;
    ctx.beginPath(); ctx.arc(x, y, radius, Math.PI * 0.75, Math.PI * 2.25); ctx.stroke();
    for (let i = 0; i <= 8; i++) {
      const angle = Math.PI * 0.75 + i / 8 * Math.PI * 1.5;
      ctx.strokeStyle = i > 6 ? '#b85242' : '#dce8ed'; ctx.lineWidth = 6;
      ctx.beginPath(); ctx.moveTo(x + Math.cos(angle) * 192, y + Math.sin(angle) * 192);
      ctx.lineTo(x + Math.cos(angle) * 213, y + Math.sin(angle) * 213); ctx.stroke();
      ctx.textAlign = 'center'; ctx.font = '30px Arial'; ctx.fillStyle = '#dce8ed';
      ctx.fillText(String(Math.round(i / 8 * maximum)), x + Math.cos(angle) * 153, y + Math.sin(angle) * 153 + 10);
    }
    const needle = Math.PI * 0.75 + value / maximum * Math.PI * 1.5;
    ctx.strokeStyle = '#f15b43'; ctx.lineWidth = 10;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(needle) * 177, y + Math.sin(needle) * 177); ctx.stroke();
    ctx.fillStyle = '#e0e9ed'; ctx.beginPath(); ctx.arc(x, y, 17, 0, Math.PI * 2); ctx.fill();
    ctx.font = '28px Arial'; ctx.fillText(title, x, 520);
  }
}

function texture(canvas: HTMLCanvasElement) {
  const map = new THREE.CanvasTexture(canvas); map.colorSpace = THREE.SRGBColorSpace; map.anisotropy = 8;
  return map;
}
