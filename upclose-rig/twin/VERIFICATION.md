# Digital twin verification

All rendered evidence is **synthetic**. See [ASSETS.md](ASSETS.md) for licensing and attribution.

The browser check (`npm test`) validates damage config rejection, projected surface placement, driver/passenger depth occlusion, bounded visible boxes, all three damage types, and viewer controls without browser errors. `npm run build` checks TypeScript and produces the Vite build.

`npm run render:zones` regenerates guided clips and validates each using ffprobe: H.264, 1280?720, 30 fps, 12 seconds and 360 decoded frames. Receipts and decoded midpoint previews are retained in `out/zones/`. The manifest lists rendered and unsupported zones, hashes and filenames. Inspect previews to assess framing; passing media checks alone does not establish useful coverage.

`npm run render` produces a separate 36-second 1920?1080 walkaround. Its audit script checks source/video hashes, timestamps, camera matrices, intrinsics and bounds. Source changes invalidate historical hash receipts; regenerate evidence when current-source provenance is required.

Known limitations: dent is shading rather than deformation. Label visibility uses depth-tested alpha fragments, with glass conservatively opaque and no ID-pass antialiasing. Exterior decals do not establish actual vehicle condition. Engine bay and underbody are explicitly not rendered. Dashboard and cabin views show simplified geometry; they do not assess electrical operation, rear seats or headliner. No model-accuracy claim is made.

## Verified local run

On 2026-11-07, `npm ci`, `npm test`, `npm run build`, and `npm run render:zones` passed. The build reports the existing large-bundle warning. The clips contain 2,160 decoded frames. Source/video hashes, timestamps, finite poses/matrices, intrinsics and bounds were checked. All six midpoint previews, body endpoints and four tire corners were visually inspected.

| Zone | Codec | Dimensions | FPS | Duration (s) | Decoded frames | Bytes |
|---|---|---|---|---|---|---|
| driver_side | h264 | 1280x720 | 30/1 | 12.000000 | 360 | 3157439 |
| passenger_side | h264 | 1280x720 | 30/1 | 12.000000 | 360 | 2870851 |
| front_rear_body | h264 | 1280x720 | 30/1 | 12.000000 | 360 | 2001419 |
| tires | h264 | 1280x720 | 30/1 | 12.000000 | 360 | 4412509 |
| dash_electrical | h264 | 1280x720 | 30/1 | 12.000000 | 360 | 1956411 |
| engine_bay | not_rendered | n/a | n/a | n/a | n/a | n/a |
| underbody | not_rendered | n/a | n/a | n/a | n/a | n/a |
| interior | h264 | 1280x720 | 30/1 | 12.000000 | 360 | 1790418 |
