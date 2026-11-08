# End-to-end verifier

A Playwright (TypeScript) project that drives the whole UpClose flow against a running stack: consent, guided capture with real video uploads, capture-kit evidence from the rig, finalize, and the free and full report. Every run writes a folder you can inspect by eye.

## Setup

```bash
cd e2e
npm install
npx playwright install chromium
```

The stack must be up (web on 5173, API on 8000, fixture mode). The harness waits for `GET /ready` and tolerates restarts. It never starts or stops containers.

ffmpeg comes from the `ffmpeg-static` package; nothing needs to be installed. The rig needs its Python environment (`upclose-rig/.venv`).

## Commands

| Command | What it does |
|---|---|
| `npm run e2e` | The full journey as one test with named steps |
| `npm run e2e:camera` | Records one zone through the in-page recorder using a fake camera fed from a clip |
| `npm run clips` | Prints which clips would be used, generating placeholders if needed |
| `npm run typecheck` | Type-checks the harness |

## Manual run (you drive the browser)

```powershell
cd e2e
npm run manual
```

This opens a visible browser (own profile, video recorded to `out/manual-<timestamp>/`) and a terminal menu. You do all the clicking; the menu handles the parts that need the rig or the operator:

| Key | Action |
|---|---|
| `p` | Attach pod and OBD evidence to the current inspection (uses `E2E_POD_PORT`, default `COM3`; set `sim` for a simulated pod). Keep the pod still 20 s until NORMAL, then shake or tap it and press the button once. |
| `a` | Preview, then approve, evidence images. Needs `OPERATOR_TOKEN` in the environment, or `OPERATOR_TOKEN_FILE` pointing at a file that holds it. |
| `s` | Print the session id (the owner token is never printed). |
| `q` | Close the browser (video is saved) and print the run folder. |

Start an inspection in the browser before pressing `p`. `npm run manual -- --selftest` runs a non-interactive check with a simulated pod.

## Run it yourself (visible browser)

From a normal PowerShell terminal at the repository root, with the stack already up:

```powershell
Set-Location e2e
$env:E2E_HEADED = '1'
$env:E2E_MODE = 'fixture'
$env:E2E_POD_PORT = 'sim'
$env:E2E_PROFILE = 'wave2'
npm run e2e
```

A Chromium window opens and actions run slightly slowed so you can follow along. Unset the variable (`Remove-Item Env:E2E_HEADED`) to go back to headless. The engine-audio step ticks the "sample or simulated clip" box first, so the clip is labelled `synthetic`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `E2E_HEADED` | unset | `1` shows the browser window |
| `E2E_PROFILE` | `wave1` | Assertion set, see below |
| `E2E_MODE` | `fixture` | `fixture` is fast. `live` is for real model providers: processing waits are 40 minutes, the test timeout is 4 hours, and an operator-approval step runs |
| `E2E_ZONES` | all | Comma list of zones to film, for example `driver_side,tires`. Other zones are left unfilmed and reported as not checked |
| `E2E_POD_PORT` | `sim` | `sim` for the simulated pod, or a serial port such as `COM3` for the real pod. With a real pod the harness prints a "tap the pod now" banner with a countdown, runs at real time, and puts the same cue in the video caption |
| `E2E_CLIPS_DIR` | `upclose-rig/twin/out/zones` | Folder with the zone clips and `manifest.json` |
| `E2E_WEB`, `E2E_API` | `http://localhost:5173`, `http://localhost:8000` | Stack addresses |
| `OPERATOR_TOKEN` | unset | Required for the live-mode approval step. Read from the environment only; missing it fails the run |

Voice and narration providers are never switched on by this harness; whatever the stack runs with is what is tested.

## Clips

Zones listed as `not_rendered` in the manifest are skipped on purpose and the report must show them as not checked. If no manifest exists, the harness makes 8 s, 720p placeholder clips with ffmpeg (`testsrc2`, burned-in text "SYNTHETIC PLACEHOLDER") in `.cache/clips/`, and leaves the engine bay and underbody unfilmed so the not-checked path is still exercised.

## Journey steps

1. Wait for the stack. 2. Open the app, consent, start a guided inspection. 3. Read the session id and owner token the way the app stores them (kept in memory only). 4. Upload each zone's clip through its file input. 5. Wait for processing by polling the API and the UI. 6. Run the rig's `demo-run` with the owner token in the child's environment (falls back to `bundle`/`post` and `obd-snapshot`/`post` if `demo-run` is missing). 7. Check the capture-kits panel. 8. Attach engine audio through the UI (`engine-audio-file-input`) with the rig's synthetic WAV from `simulate-audio`; a missing control or a rejected file fails the step. 9. Finalize and wait for the report. 10. Free view checks. 11. Unlock (the demo unlock) and check the full report.

## Profiles

`wave1`
- the report renders;
- the kits section shows `rig_telemetry` and `obd_snapshot` with synthetic badges;
- zones that were not captured say not checked;
- the free view shows no DTC codes, VIN, times, audio or spectrogram URLs, checked in both the HTML and the JSON.

`wave2` adds
- Visual, Mechanical and Overall category scores: valid states, a number only when assessed, the overall basis, and the page showing what the API says (also in the server HTML);
- the Mechanical checks panel (sensor pod and OBD read "Checked", engine audio row present, score breakdown when the API sends one);
- the vibration chart SVG, in the owner report and in the server HTML;
- the dashboard block: the free view carries only `checked` and `flag_count`; the full report carries the assessment (state `assessed`, `not_readable` or `not_checked`, with the crop withheld). With the fixture reader only the shape and state are asserted, not which lights are lit;
- the advisory block with its label, the "generated advice" note and, in fixture mode, a synthetic badge;
- synthetic badges on the sensor pod, OBD, dashboard, advisory and narration;
- engine bay and underbody shown as not checked (coverage, "What we couldn't check" and the zones table);
- the free view leaking no DTC code, VIN, dashboard crop or indicator name, in both the JSON and the HTML, and carrying no vibration series or advisory;
- the narration audio element and the engine-audio spectrogram;
- the engine-check guidance controls and the voice-guide catalog loaded during capture.

Every required assertion must pass. A failed critical step blocks dependent checks and fails the run. A step deliberately excluded by the selected profile or clip manifest is `not-checked` and is never counted as a pass. Report UI assertions run on the owner report shown by the app.

## What a run leaves behind

`out/<UTC timestamp>/`
- `summary.json`: pass or fail per step, with durations, notes and the paths used;
- `screenshots/`: one per step;
- `run.mp4`: the whole run in one video (H.264, yuv420p, 30 fps) with a caption for the current step and a running clock;
- `video-1.webm` and `trace.zip`: the raw recording and the Playwright trace;
- `inputs/`: every simulated input that went in (clips with `manifest.json` and sha256, the pod recording and verdict, the OBD snapshot, the engine audio and its sidecar, the rig's JSON step lines) plus `INDEX.md` listing each file, its provenance (synthetic, cached or live) and where it came from;
- `rig-output.txt`.

The owner token is never written to disk: the trace is rewritten with the token blanked, and at the end every file in the run folder is scanned for it (`tokenOnDisk` in `summary.json`).

The camera test disables Playwright video recording and tracing to isolate the in-page camera recorder. It keeps screenshots, the summary, and the token scan; its recorded clip must still upload and reach the processed state. The journey keeps the full artifact set above.
