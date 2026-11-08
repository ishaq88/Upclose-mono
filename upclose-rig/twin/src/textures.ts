import { CanvasTexture, SRGBColorSpace } from 'three';
import type { Damage } from './types';

// Original deterministic procedural artwork. No downloaded damage photographs.
export function damageTexture(type: Damage['type']): CanvasTexture {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 512;
  const ctx = canvas.getContext('2d')!;
  let seed = 7331;
  const random = (): number => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
  if (type === 'scratch') {
    for (let line = 0; line < 14; line++) {
      const start = 40 + random() * 110;
      const y = 160 + random() * 190;
      ctx.beginPath(); ctx.moveTo(start, y);
      ctx.bezierCurveTo(220, y - 18, 320, y + 30, 395 + random() * 75, y - 25 + random() * 50);
      ctx.strokeStyle = line % 3 ? '#c9c5b7d8' : '#3a3935dc';
      ctx.lineWidth = 1.3 + random() * 4; ctx.stroke();
    }
  } else if (type === 'rust') {
    const data = ctx.createImageData(512, 512);
    for (let y = 0; y < 512; y++) for (let x = 0; x < 512; x++) {
      const u = (x - 256) / 220, v = (y - 256) / 205;
      const angle = Math.atan2(v, u);
      const edge = 0.82 + 0.09 * Math.sin(angle * 7) + 0.06 * Math.cos(angle * 13);
      const r = Math.hypot(u, v), noise = random();
      const index = (y * 512 + x) * 4;
      const alpha = Math.max(0, Math.min(1, (edge - r) * 15 + noise * 0.2));
      data.data[index] = 75 + noise * 100;
      data.data[index + 1] = 33 + noise * 43;
      data.data[index + 2] = 15 + noise * 20;
      data.data[index + 3] = alpha * 255;
    }
    ctx.putImageData(data, 0, 0);
  } else {
    // A shading decal, not physical displaced geometry (explicit in metadata).
    const dark = ctx.createRadialGradient(233, 235, 15, 256, 256, 205);
    dark.addColorStop(0, '#18323970'); dark.addColorStop(0.6, '#13262e60'); dark.addColorStop(1, '#172a3000');
    ctx.fillStyle = dark; ctx.fillRect(0, 0, 512, 512);
    ctx.beginPath(); ctx.ellipse(263, 275, 152, 141, -0.3, 0.12, Math.PI * 0.94);
    ctx.strokeStyle = '#eff6e79c'; ctx.lineWidth = 9; ctx.filter = 'blur(9px)'; ctx.stroke(); ctx.filter = 'none';
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}
