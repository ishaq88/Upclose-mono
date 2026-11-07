# upclose-rig

Capture-rig plugin for UpClose (used-car inspection). It produces two evidence sources that UpClose accepts before an inspection is finalized:

| Kind | Producer |
|---|---|
| `rig_telemetry` | Seeed Grove Beginner Kit pod (accelerometer, sound, temperature) **or its simulator** |
| `obd_snapshot` | python-OBD reading an **emulated ELM327** (no car, no dongle) |

The wire format is the contract in the UpClose repo, `.docs/ubhacking-evidence-sources.md`: `POST /api/v1/sessions/{session_id}/sources`, owner bearer token. The models in `src/upclose_rig/models.py` mirror it (strict, `extra="forbid"`) and every bundle is validated client-side before it is written or sent.

**Everything works without the board.** The simulator emits the same records as the firmware, and simulator/board recordings share one file format, so a real recording drops in with no code change.

> **Provenance:** simulated, bench, or emulated data must be posted with `--provenance synthetic`. The tool refuses to label simulator recordings or the emulated OBD adapter as anything else. `cached` = replay of a real recording, `live` = streamed from a real vehicle now. Telemetry from a sensor-on-a-box on the bench is `synthetic` too.

## Setup

```powershell
cd G:\upclose-rig
python -m venv .venv                      # Python 3.12
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

Dependencies: pyserial, obd (python-OBD), ELM327-emulator, pydantic, httpx, numpy (simulator), pytest.

## Board setup (H03)

Hardware: Seeed Grove Beginner Kit for Arduino (ATmega328P, micro-USB). Everything is pre-wired on the PCB:

| Module | Pin / bus |
|---|---|
| LIS3DHTR accelerometer | I2C 0x19 |
| DHT20 temperature/humidity | I2C 0x38 |
| OLED 0.96" SSD1315 | I2C 0x3C |
| Sound sensor | A2 |
| Rotary knob | A0 |
| Button | D6 (active high) |
| LED | D4 (mirrors the button) |
| Buzzer | D5 (kept silent) |

Arduino IDE steps:

1. Tools > Board > **Arduino Uno**; Tools > Port > the kit's COM port.
2. Library Manager: install **U8g2** (by oliver, "U8g2" / olikraus). `Wire` is built in. No Seeed library is needed: the LIS3DHTR is driven by raw register I2C (lighter, no float math, FIFO access) and the DHT20 by a small non-blocking state machine (the stock library blocks about 80 ms).
3. Open `firmware/grove_pod/grove_pod.ino`, Upload.
4. Serial Monitor at **250000** baud should show `#upclose-pod v1` then records. Close it before using the bridge (one program per port).

> The sketch has **not been compiled or run on hardware** (no arduino-cli here). Expect to fix a typo or two on first compile. It targets 2 KB RAM: no `String`, no printf, one 48-byte record buffer.

OLED: shows `streaming`, knob as RPM (600-3000), button/FAULT, temperature/humidity, measured accel rate. At most one text row is redrawn per loop pass, about 2 Hz overall.

The accelerometer runs at 400 Hz, +/-4 g, 12-bit (2 mg/digit) with its 32-sample FIFO in stream mode, drained every ~5 ms, so OLED updates cannot drop samples.

> **Measured resolution (first real run, pod still on a desk):** the delivered values step in **8 mg**, not the 2 mg of 12-bit high-resolution mode. 8 mg per digit at +/-4 g is the LIS3DH datasheet's 10-bit normal-mode sensitivity (from memory of the ST table; not re-checked against the datasheet here), so the HR bit set by the sketch (`CTRL_REG4 = 0x98`) does not appear to be taking effect on this board. Cause not established; reading `CTRL_REG4` back over I2C would show it. The anomaly code does not depend on fine resolution, but a still pod has only 8-15 distinct values per axis, so do not read fine detail into the low bits. Timestamps are spread between drains, so the average rate equals the real rate.

## Line protocol v1 (250000 baud, one record per line)

```
#upclose-pod v1                    header, first line after every reset
A,<t_ms>,<x_mg>,<y_mg>,<z_mg>      accel, ints, ~400 Hz
S,<t_ms>,<level>                   sound 0..1023, ~100 Hz (peak-to-peak of a short ADC burst)
T,<t_ms>,<celsius>,<rh>            every 2 s, floats with one decimal
B,<t_ms>,<0|1>                     button edge (1 = pressed), 20 ms debounce
K,<t_ms>,<0..1023>                 knob, on change >= 8 counts, max 10 Hz
#...                               comment lines (e.g. `#err accel not found`) are ignored
```

`t_ms` is `millis()` since reset. A missing or wrong-version header is an error; garbled lines are counted and skipped. The format is versioned by the header (`v1`).

## Recording file

JSON Lines (same for `.jsonl` / `.upr`; `*.upr` and `recordings/` are git-ignored). Line 1 is a header object
(`format`, `source` = `board`|`simulator`, `protocol`, `started_at`, plus simulator params); then one JSON array per record: `["A",12,-4,9,1001]`.

## Demo flow (no hardware)

```powershell
$py = ".\.venv\Scripts\python"

# 1. synthetic idle engine, 780 rpm -> dominant vibration at 26 Hz (4 cyl: rpm/30)
& $py -m upclose_rig simulate --seconds 60 --rpm 780 --out recordings\idle.upr
# misfiring, cold start, scripted button press + knob
& $py -m upclose_rig simulate --seconds 60 --rpm 780 --misfire-every 4 --cold `
    --event K@5=300 --event B@20=1 --event B@22=0 --out recordings\misfire.upr

# 2. contract JSON (written to a file, or printed without --out)
& $py -m upclose_rig bundle --recording recordings\misfire.upr --provenance synthetic --out recordings\misfire.bundle.json

# 3. emulated OBD: stored P0301, then the "seller cleared the codes" variant
& $py -m upclose_rig obd-snapshot --dtc P0301 --out recordings\obd.json
& $py -m upclose_rig obd-snapshot --cleared --out recordings\obd_cleared.json
# link: button press in a recording injects P0301, knob sets RPM (600-3000)
& $py -m upclose_rig obd-snapshot --from-recording recordings\misfire.upr --out recordings\obd_linked.json

# 4. post (session must be in `capturing`; the token is read from the environment, never argv)
$env:UPCLOSE_OWNER_TOKEN = "<owner token>"
& $py -m upclose_rig post --recording recordings\misfire.upr --provenance synthetic `
    --api http://localhost:8000 --session <session-uuid>
& $py -m upclose_rig post --body recordings\obd.json --api http://localhost:8000 --session <session-uuid>
```

`post` reports: 201 created (source id, sha256, summary), 409 session already finalized, 401 token rejected, 404 session not found/deleted, anything else with the status and a short detail. The token is never printed. One source per (session, kind); re-posting while capturing replaces it.

## With the board

```powershell
& $py -m upclose_rig monitor --port COM5                         # live values + rates, for calibration
& $py -m upclose_rig record  --port COM5 --seconds 60 --out recordings\box.upr
& $py -m upclose_rig bundle  --recording recordings\box.upr --provenance synthetic   # box on a bench
& $py -m upclose_rig post    --recording recordings\box.upr --provenance cached --session <uuid>
```

Use `cached` only for a replay of a real engine recording and `live` only when streaming from a real vehicle now. `--start-s` / `--seconds` select a window (needed above 240000 accel samples, i.e. 10 minutes at 400 Hz).

How bundling maps data: accel/sound `rate_hz` is `(n-1) / (t_last - t_first)` from timestamps; temperature `t_ms` is relative to the window start (= `captured_at`); out-of-range values are rejected, not clipped; `cylinders` defaults to 4 (`--cylinders`).

## Emulated OBD (H05)

`obd_sim.py` runs the **ELM327-emulator** package in-process on a loopback TCP port; python-OBD connects with `socket://127.0.0.1:<port>` (fixed 38400 baud, because python-OBD's auto-baud probing sends bytes the emulator rejects). A private scenario is cloned from the package's `car` scenario and only the state-driven requests are overridden: PID 01 (MIL, DTC count, readiness bits), 05 coolant, 0C RPM, 30 warm-ups, 31 distance, modes 03/07 (DTCs), 04 (clear), 09 02 (VIN). The state is a mutable `CarState`; a mode-04 clear (what a seller does) resets the DTCs and MIL, distance and warm-ups to 0, and marks the non-continuous monitors incomplete. The VIN is synthetic (`SYNTHETC00000UPCA`; valid per the contract regex, which excludes I, O, Q).

```powershell
& $py -m upclose_rig obd-serve --port 35000 --dtc P0301     # keep running; connect any python-OBD client
& $py -m upclose_rig obd-snapshot --obd-port COM7 --provenance live   # same query path against a real adapter
```

## Engine anomaly model (H16)

Baseline-relative anomaly detection on the pod's vibration (and optional audio). No labeled car-fault data exists, so the model learns what *this engine* looks like when healthy and scores how far new 0.5 s windows (50 % overlap) deviate. numpy only. It says "unlike the baseline", not "faulty".

```powershell
& $py -m upclose_rig simulate --seconds 60 --rpm 780 --out recordings\healthy.upr
& $py -m upclose_rig anomaly-fit   --recording recordings\healthy.upr --out recordings\model.json   # >= 20 s, healthy, at working speed
& $py -m upclose_rig anomaly-score --model recordings\model.json --recording recordings\misfire.upr   # prints verdict JSON
& $py -m upclose_rig anomaly-eval                                                                    # regenerates evalnomaly_report.md
```

`anomaly-fit` and `anomaly-score` also take `--audio clip.wav` (mono or stereo PCM WAV, resampled to 16 kHz, starting with the recording). A model fitted with audio requires audio when scoring.

Model: median/MAD standardization (with a per-feature minimum scale), Ledoit-Wolf-shrunk covariance, Mahalanobis distance. Alternate 8-window baseline blocks fit the model; the other blocks set the threshold (99th percentile of held-out baseline scores). Models are JSON (no pickle). Evaluation numbers, data provenance and limits are in `eval/anomaly_report.md`: **synthetic simulator data only, not real-car accuracy.**

Verdict JSON (`schema: "upclose-anomaly-verdict/1"`):

| Field | Meaning |
|---|---|
| `verdict` | `anomaly_suspected` \| `no_anomaly_detected` \| `not_assessed` |
| `reason` | `null`, or why `not_assessed`: `too_short`, `flat_signal`, `clipped_signal`, `non_finite_signal`, `sample_rate_mismatch`, `no_accel`, `audio_required_by_model`, `audio_too_short`, `non_finite_features` |
| `data_source` | recording header `source` (`simulator` / `board`); the caller sets provenance |
| `duration_s`, `windows` | recording length, number of windows scored (null when not assessed) |
| `anomalous_windows`, `anomalous_fraction` | windows above `threshold` (null when not assessed; `max_score` and `median_score` are null too) |
| `max_score`, `median_score`, `threshold`, `fraction_threshold`, `min_anomalous_windows` | Mahalanobis distances; suspected when at least `min_anomalous_windows` (3) windows and `fraction_threshold` (0.15) of windows exceed `threshold` |
| `top_features` | `[{"name", "share"}]` features contributing most to the anomalous windows (empty unless suspected) |
| `anomalous_runs` | up to 10 contiguous runs: `t_start_s`, `t_end_s`, `peak_score`, `top_features` (with `z`, robust-sigma units) |
| `model` | `fire_hz`, `fs_hz`, `has_audio`, `baseline_seconds`, `baseline_source`, `threshold_percentile`, `config_hash` (sha256 of the model JSON) |
| `note` | fixed disclaimer: relative to the baseline, not a diagnosis, not calibrated accuracy |

Quality rules: `flat_signal` = AC standard deviation under 1.5 mg on every axis, or more than 95 % of consecutive samples identical. `clipped_signal` = at least 2 % of an axis' samples sit on one extreme that is within 10 % of the +/-4000 mg full scale (or more than 1 % of all values at or beyond 3990 mg). A quiet signal that repeats its own min and max because of 8 mg quantization is not clipped (this was a real bug: it made a still pod read `clipped_signal`). `not_assessed` for bad quality is latched in `StreamScorer` until the signal has been clean for about 1 s, so the pod does not flip between `N` and `X`; `StreamScorer.pod_state` is the single source for the pod display.

Bad input is never a clean score: short, flat, clipped, wrong-rate or non-finite recordings give `not_assessed`. Scores are distances, not probabilities.

Live use (`upclose_rig.anomaly.StreamScorer`): `push(records)` with parsed pod records; it baselines the first `baseline_s` seconds (default 30, min 20) and then returns events for the H17 "Act" step: `baseline_ready`, `anomaly_suspected` (3 of the last 5 windows anomalous; includes `score`, `top_features`), `anomaly_cleared` (8 normal windows in a row), `not_assessed` (with `reason`). Vibration only; the baseline period is assumed healthy.

## Live alerts on the pod (H17: Act)

`live` closes the Sense -> Understand -> Act loop: it reads the pod stream, baselines the first N seconds with `StreamScorer`, then scores live and tells the **pod** what to show.

```powershell
& $py -m upclose_rig live --port COM3 --baseline-s 20 --record recordings
un.upr          # real pod (close the Serial Monitor first)
& $py -m upclose_rig live --port sim --seconds 60 --record recordings\sim.upr             # simulated pod, real time, no hardware
& $py -m upclose_rig live --port sim --speed 0                                            # same, as fast as possible
& $py -m upclose_rig bundle --recording recordings
un.upr --verdict recordings
un.verdict.json --out recordings
un.bundle.json
```

`--port sim` runs the simulator through the same loop (SYNTHETIC): healthy engine, a misfire from `--sim-misfire-at` (default 35 s) for `--sim-misfire-for` (default 12 s), and prints each command that would go to the pod. `--mute` starts with the buzzer muted. Start the baseline with the engine idling healthy; the model only knows what that period looked like.

**Host -> pod commands** (extension of `upclose-pod v1`; one ASCII line each, at most 23 characters, newline-terminated; the pod ignores malformed lines):

| Line | Meaning | Pod shows |
|---|---|---|
| `!S,L<seconds_left>` | baseline running | `LEARNING <n>s` |
| `!S,N` | normal | `NORMAL` |
| `!S,A` | anomaly suspected | `MISFIRE` / `SUSPECTED`, LED blinks, buzzer chirps once a second |
| `!S,X` | not assessed (flat or clipped signal, failed baseline) | `NOT ASSESSED` |
| `!M,<0\|1>` | buzzer mute off / on | `mute` on the last row |

The pod acknowledges an accepted command with a comment line `#ack,<body>` (for example `#ack,S,A`), so older hosts ignore it. The bridge re-sends the state about once per second of stream time; the pod returns to idle if it hears nothing for 5 s. A **button hold of 1 s or more** toggles mute on the pod (and sends `#ack,M,<0|1>`); a short press keeps its meaning (the `B` edges, which inject P0301 in the OBD link). Chirps use the Timer2 `tone()` schedule and never block the sampling loop.

Alert rule (`StreamScorer`): suspected when at least 3 of the last 5 windows exceed the baseline threshold and their mean score is at least 1.5x the threshold; cleared after 8 normal windows. This is a persistence rule on a distance, not a probability.

At the end `live` writes `<recording>.verdict.json` (or `--verdict-out`): the verdict over the post-baseline part of the stream, run times relative to the recording start, `model.config_hash` (sha256 of the fitted model JSON), `baseline_seconds`, window counts. No verdict is written when the baseline never completed. `bundle` / `post` accept `--verdict <file>` and embed it as `rig_telemetry.data.model_verdict`; it is validated against the contract (bounded lists, finite numbers, `anomalous_fraction == anomalous_windows / windows`, verdict consistent with the stated thresholds, `not_assessed` with a reason and null counts) before anything is sent. Without `--verdict` the body is unchanged.

## Safety (real engine use)

- Attach the pod with the **engine off**, and fix it so it cannot move into moving parts.
- Route the USB cable away from belts, fan, pulleys and hot manifolds; strain-relieve it. Keep the laptop outside the engine bay.
- The pod is not rated for engine-bay heat: stop if the cover is too hot to hold. Never drive with it attached.
- OBD: never post `live` unless the data came from a real vehicle now.

## Layout

```
firmware/grove_pod/grove_pod.ino   H03 firmware
src/upclose_rig/anomaly/           H16: features, model, stream, evaluate
eval/anomaly_report.md            H16 synthetic evaluation (generated)
src/upclose_rig/                   H04/H05: protocol, recording, simulator, models, bundle, serial_io, post, obd_sim, obd_snapshot, cli
tests/                             pytest (no hardware, no network)
```
