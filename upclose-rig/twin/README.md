# UpClose digital twin

A local Vite + three.js inspection lab. A licensed Ferrari GLB, Poly Haven studio illumination, neutral floor and shadows, and three original projected damage textures produce **synthetic** evidence. This is a controlled visibility fixture, not a real car assessment or validation of AI accuracy.

## Install and preview

Run from `upclose-rig/twin` with Node 22.12+ (verified toolchain/results in `VERIFICATION.md`). No Docker, API keys or host ffmpeg required.

```powershell
cd upclose-rig/twin
npm ci
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD/out/.browser-cache"
npx playwright install chromium
npm run dev -- --port 5178
```

Open http://127.0.0.1:5178. Drag to orbit, scroll to zoom, toggle markers, select a placed damage entry or marker, and preview the walkaround. Assets and Draco decoder are local; preview/render makes no external asset requests. The dev server binds loopback only. Use `npm run build` for TypeScript checks and a production build.

## Render and verify

```powershell
npm test
npm run render
```

The render script starts its own temporary loopback Vite server and headless Chromium, captures exactly 1080 deterministic frames at **1920×1080 / 30 fps / 36 seconds**, and encodes H.264/yuv420p with the pinned `ffmpeg-static` binary. Camera height is 1.2 m with 1.8 cm bob; one orbit varies angular speed smoothly to dwell at each panel. Render duration is independent of machine speed. Windows uses the locally verified Direct3D11 backend; other platforms use Chromium's default backend. The renderer name is persisted in ground truth. For troubleshooting, set `TWIN_ANGLE=default` to use Chromium's default or `TWIN_ANGLE=swiftshader` for software rendering. `node scripts/benchmark.mjs` prints the backend and capture time for five frames. Software rendering can take substantially longer. Output must remain below 200 MiB or the command fails.

The default command creates a timestamped render ID. To choose one explicitly, use `npm run render -- --id my-walkaround`. Both color and ID shaders are compiled before capture readiness, including cold GPU startup.

Optional quick smoke: `npm run render -- --id short-smoke --seconds 2`. This accelerates the full orbit into 2 seconds, so it does **not** meet the 30–45 second acceptance requirement. IDs cannot reuse existing output directories. No automatic uploads or inference calls occur.

Outputs in `out/<id>/` (gitignored):

- `walkaround.synthetic.mp4`: labeled footage with attribution.
- `ground_truth.json`: one entry per encoded frame, input and video hashes, damage config, resolved surface placements, camera calibration/poses, and visible damage boxes.
- `scene.json`, `damage.json`, `ASSETS.md`: frozen configuration and attribution.
- `check/frame-NNNN.png`, `.overlay.png`, `.json`: three decoded MP4 frames, their visible boxes drawn in orange, and associated camera/label data. The selected frame maximizes visible pixel area for each damage item; all three must be visible at least once.
- `check/video-verification.txt`: ffmpeg decode receipt proving resolution, fps and decoded frame count.
- `check/summary.json`: machine-check results. Visual overlay alignment is a separate human/agent inspection, not implied by this JSON.

After export, run `node scripts/audit.mjs out/<id>` from the twin root to validate the video and current input hashes plus every label's timestamp, pose, calibration and bounds. It writes `check/metadata-audit.json`. The completed local run and exact results are recorded in [VERIFICATION.md](VERIFICATION.md).

## Coordinate and ground-truth contract

World coordinates are meters: Y up, front -Z, driver side -X (left-hand-drive example). Camera looks down local -Z; local +Y is up. Matrices use three.js/OpenGL column-major order. Every frame includes camera position, quaternion `[x,y,z,w]`, camera-to-world and world-to-camera 4×4 matrices, plus pixel `fx`, `fy`, `cx`, `cy`, width and height. For a camera-space point `(x,y,z)` in front (`z<0`), `u=fx*x/(-z)+cx`, `v=cy-fy*y/(-z)`.

`visible_damage[].bbox_xyxy` is `[left,top,right,bottom]` in pixel coordinates, with exclusive right/bottom, clipped to frame. It encloses **visible** decal fragments, not the whole projector rectangle. A second full-resolution GPU ID pass uses the same camera, decal mesh, texture alpha threshold (0.10), and polygon offset. The full car and ground write depth; depth-hidden or transparent parts of the decal contribute no label pixels. Glass is conservatively opaque in this pass. The ID pass is not antialiased; visible color edges can differ by a pixel. `visible_pixels` is the fragment count. An item with zero visible pixels is omitted for that frame, never declared clean. Markers and future finding pins are excluded from video and labels.

Known limits: damage is synthetic surface artwork; dent is shading rather than deformation. No claims about real-world recognition or severity follow. This exterior walkaround does not cover the engine bay, dashboard, interior or underbody. Pipeline inference and evaluation are separate; this renderer does not call them.

## Change the scene

`scene.json` has one `model` entry: change its `url` to a local `/assets/your-car.glb`. The default normalization grounds and centers a Y-up car to `lengthMeters`; set `rotationY` (radians) and `bodyMesh` for different source geometry. `paint` controls the replacement body material. These settings and adjusted damage coordinates should be kept together for reproducibility. A missing model, body mesh, HDRI or damage/surface intersection fails visibly rather than silently falling back.

`damage.json` entries specify `id`, `type` (`scratch|dent|rust`), a canonical UpClose `zone`, `position`, outward unit `normal`, and projector `size` (`width,height,depth` in meters). A short ray projects the seed position onto the named body mesh; actual surface coordinates are persisted as `placements`. Projected triangles whose average normal opposes the configured outward normal are discarded so exterior damage cannot stain the inner panel skin. Reposition entries when swapping car topology. Valid zones mirror `services/api/app/contracts/enums.py`: `driver_side`, `passenger_side`, `front_rear_body`, `tires`, `dash_electrical`, `engine_bay`, `underbody`, `interior`. There are no separate `front`/`rear` zone strings.

Phase 2: `addFindingPins(pins: readonly FindingPin[])` is exported from `src/main.ts` and exposed at `window.twin.addFindingPins`. `FindingPin` is defined in `src/types.ts`: id, label, position, canonical zone, evidenceId, and validated verdict (`confirmed|unverifiable`). It is a minimal clickable display hook; it does not fetch reports, infer verdicts, map 2D detections to 3D, or grant access to evidence.

Asset credits and source/license evidence: [ASSETS.md](ASSETS.md).

## Guided capture clips

Run `npm run render:zones` to regenerate `out/zones/`. Six zones follow the app guidance: driver side rear to front, passenger side front to rear, combined front/rear body, a low pass around all four tires, a parked dashboard view, and a cabin pan. Exterior and interior clips are **12 seconds, 1280x720, 30 fps, H.264/yuv420p**, with deterministic slight sway and no audio. Dashboard variants are 9 seconds each. Tire geometry is illustrative; it cannot establish real tread depth or condition.

Every frame carries SYNTHETIC and asset attribution. Outputs include `<zone>.mp4`, `ground_truth_<zone>.json`, ffprobe JSON receipts and decoded midpoint previews. `manifest.json` lists all eight canonical zones in capture order, clip hashes, visible damage IDs and input/model/asset hashes. Unsupported zones have `status: not_rendered`, a reason, null media fields and empty ground-truth frames. The engine bay and underbody lack inspectable geometry: show these as not checked. The dashboard uses a procedural cluster with synthetic gauges and emissive warning icons; it supports no real electrical assessment. This open-top two-seat car has no rear seats or headliner.

The command verifies codec, dimensions, fps, duration and decoded frame count with pinned `ffprobe-static`. It replaces its own named outputs and publishes the manifest after all clips pass. Poses and visibility are deterministic for the same inputs/backend; encoded bytes can differ across graphics drivers or encoder platforms. `npm run render` still produces the full 36-second 1080p walkaround.

### Dashboard warning fixtures

`npm run render:zones` also writes a **9-second** `dash_electrical.mp4` with amber `check_engine`, red `oil_pressure` and amber `tpms` lamps, plus `dash_electrical_clean.mp4` with no lit warning lamps. Both use the same steady driver-seat camera, slight hand shake and a tachometer reading 1,800 RPM. The cluster and icon artwork are original procedural drawings mounted in the cockpit; the lamps use emissive materials. Gauge values and lamp states are synthetic ground truth.

The eight canonical zones remain in `manifest.zones`. The clean clip is an extra entry in `manifest.variants` with canonical `zone: dash_electrical` and `variant: clean`. Each variant has a matching ground-truth JSON with top-level and per-frame `lit_indicators` and `tachometer_rpm`. Three decoded sharp PNG stills per variant are in `out/zones/dash_stills/`, with filenames, frame numbers, timestamps and hashes listed in the manifest. The frames are 30, 135 and 240 (1, 4.5 and 8 seconds).
