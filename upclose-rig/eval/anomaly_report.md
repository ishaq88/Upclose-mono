# Engine anomaly model: evaluation report

**All data here is SYNTHETIC**, produced by `upclose_rig.simulator`. This measures whether a baseline-relative
detector finds a simulator-style misfire (every Nth firing event dropped) on a simulator engine. It is **not**
real-car accuracy and must not be quoted as such.

Regenerate: `python -m upclose_rig anomaly-eval` (fixed seeds; the output is byte-identical between runs).

## Method

- Features per 0.5 s window (50 % overlap) from the principal-axis vibration: RMS, crest factor, kurtosis, band energy at the
  firing frequency, its half order, sub-orders, 2x and the high band, a sub-order ratio, and firing-to-firing amplitude spread.
  The firing frequency is estimated from the baseline.
- Model: median/MAD standardization, Ledoit-Wolf-shrunk covariance (floor 0.05), Mahalanobis distance. Baseline windows are
  split in alternating 8-window blocks: even blocks fit, odd blocks calibrate the threshold
  (the 99th percentile of held-out baseline scores).
- Recording verdict: `anomaly_suspected` when at least 3 windows and 15% of windows exceed the threshold.
  `not_assessed` for short, flat, clipped, wrong-rate or non-finite input; never a clean verdict on bad input.
- A small numpy autoencoder was **not** built: with 10 features and under 120 fit windows per engine there was no data to
  justify it, and it would add a second thing to explain. Mahalanobis only.

## Data and provenance

- Conditions: RPM 780, 1500, 2400, 3000; extra Gaussian noise 0, 10, 20 mg per axis
  on top of the simulator's own 5 mg (a 12-condition grid).
- Per condition: one 60 s healthy baseline (the only thing the model sees), then 4 healthy and
  4 misfiring 30 s test recordings at each of `--misfire-every` 2, 3, 4, 6, 8, 12.
- Total: 48 healthy and 288 misfiring test recordings. Seeds start at 1000; all constants
  (window length, bands, percentile, fraction threshold) were chosen on seeds below 100 and not changed after looking at these.
- CC0 engine clips: none evaluated. `samples/` was absent when this report was generated; the audio features are
  unit-tested on synthetic audio only. Clips are audio-only and the v1 model needs a vibration baseline, so they could not
  be scored without a new audio-only mode.

## Headline (all conditions pooled)

| Metric | Value |
|---|---|
| Window-level ROC-AUC (healthy vs misfiring windows, score / threshold) | 0.997 |
| Recording-level ROC-AUC (score = fraction of anomalous windows) | 1.000 |
| Recording detection rate at the chosen threshold (misfiring flagged) | 100% |
| Recording false-alarm rate (healthy flagged) | 4% |
| Window false-positive rate / true-positive rate at threshold | 2.9% / 99.6% |
| Recordings `not_assessed` | 0 of 336 |

## By misfire severity (pooled over rpm and noise)

`--misfire-every N`: lower N = more frequent misfire. Healthy rows are the shared comparison set.

| misfire-every | recordings | window AUC | recording AUC | detection rate |
|---|---|---|---|---|
| 2 | 48 | 0.998 | 1.000 | 100% |
| 3 | 48 | 0.998 | 1.000 | 100% |
| 4 | 48 | 0.997 | 1.000 | 100% |
| 6 | 48 | 0.997 | 1.000 | 100% |
| 8 | 48 | 0.997 | 1.000 | 100% |
| 12 | 48 | 0.994 | 1.000 | 100% |

## By RPM (pooled over noise and severity)

| rpm | firing Hz | window AUC | detection rate | false-alarm rate |
|---|---|---|---|---|
| 780 | 26 | 0.999 | 100% | 17% |
| 1500 | 50 | 0.999 | 100% | 0% |
| 2400 | 80 | 0.995 | 100% | 0% |
| 3000 | 100 | 0.992 | 100% | 0% |

## By extra noise (pooled over rpm and severity)

| extra noise (mg) | window AUC | detection rate | false-alarm rate |
|---|---|---|---|
| 0 | 0.996 | 100% | 0% |
| 10 | 0.997 | 100% | 12% |
| 20 | 0.998 | 100% | 0% |

## Detection rate by condition and severity

| rpm | noise (mg) | false alarm | N=2 | N=3 | N=4 | N=6 | N=8 | N=12 |
|---|---|---|---|---|---|---|---|---|
| 780 | 0 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 780 | 10 | 50% | 100% | 100% | 100% | 100% | 100% | 100% |
| 780 | 20 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 1500 | 0 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 1500 | 10 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 1500 | 20 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 2400 | 0 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 2400 | 10 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 2400 | 20 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 3000 | 0 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 3000 | 10 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| 3000 | 20 | 0% | 100% | 100% | 100% | 100% | 100% | 100% |

## Limits

- Near-perfect detection mostly shows that the simulator's misfire (a pulse cut to 8 % amplitude) is a large, clean change. Treat the
  false-alarm column, not the detection column, as the informative one.
- Synthetic fault, synthetic engine, one fault family. The simulator misfire is a dropped pulse; real misfires, knock,
  worn bearings, belts and mounts look different, and a real engine cover resonates and drifts.
- The model compares against *this engine's* baseline at the baseline's speed. A baseline taken at a different RPM, or
  not healthy, makes the verdict meaningless; the model cannot detect that.
- High RPM at a 400 Hz sample rate leaves 4 samples per firing and a Nyquist of 200 Hz (2x firing is at the limit): see the RPM table.
- Model confidence is a distance, not a probability. Thresholds are calibrated to about a 1 % window false-positive rate on
  the baseline's own held-out blocks; the false-alarm rates above are from synthetic data with a stationary engine.
- Audio features exist and are tested on synthetic audio but are not part of these numbers.
