# Evidence sources

Capture kits attach evidence to an UpClose inspection **while it is capturing**, before it is finalized. Evidence shows up in the report as **Mechanical checks** and feeds a separate **Mechanical** score next to the **Visual** one. A kit that wasn't used shows as "Not checked" and never counts as clean.

## How the scores combine

- **Visual** is the existing inspection score from the footage.
- **Mechanical** starts at 100 and loses points for the evidence: an engine-anomaly verdict (25), each stored or permanent trouble code (15) and each pending one (8, codes capped at 45 together), the check-engine light (10), codes that look recently cleared (10), and a dashboard warning-light reading when one was assessed. Every weight is an unvalidated design default, listed in `services/api/config/mechanical.v1.json`.
- Mechanical exists only with an assessed OBD snapshot or an assessed engine-pod verdict. An unassessed verdict is never treated as clean.
- **Overall** blends the two (0.6 Visual, 0.4 Mechanical) only when both exist. Otherwise it reads "Visual only: mechanical not checked" (or the reverse) and carries no number.
- Scores appear in the free view too; codes, times, features and the point-by-point breakdown stay in the paid report. Reports built before this change keep their original bytes and hashes.

## API

`POST /api/v1/sessions/{session_id}/sources` (owner bearer token)
- Adds a source, or replaces the existing source of the same kind.
- Returns `201 {source_id, kind, data_sha256, summary}`.
- Errors: `409` once the inspection is finalized, `404` for an unknown or deleted inspection, `401` for a bad token, `422` for an invalid body.

`GET /api/v1/sessions/{session_id}/sources` (owner bearer token)
- Returns `{"sources": [{source_id, kind, provenance, device, captured_at, data_sha256, summary}]}`.
- Raw samples are never returned.

Request envelope (strict, unknown fields are rejected):

```json
{
  "kind": "rig_telemetry",
  "provenance": "synthetic",
  "device": "grove-beginner-kit",
  "captured_at": "2026-11-07T21:14:03Z",
  "data": { }
}
```

- `provenance`:
  - `synthetic`: simulator, bench stand-in, emulator or sample data.
  - `cached`: a replay of a real recording.
  - `live`: streamed from a real vehicle now.
- `device`: `^[a-z0-9][a-z0-9._-]{0,63}$`.

## Kinds

### `rig_telemetry`

```json
{
  "accel": {"rate_hz": 400.0, "x_mg": [12, 9], "y_mg": [3, 4], "z_mg": [1001, 998]},
  "sound": {"rate_hz": 100.0, "level": [312, 330]},
  "temperature": [{"t_ms": 0, "celsius": 21.4}],
  "cylinders": 4
}
```

- `accel`: `rate_hz` 50–1000; three equal-length axes; 1–240000 samples; integers within ±16000 mg.
- `sound` (optional): `rate_hz` 1–1000; `level` 0–1023.
- `temperature` (optional): up to 600 readings; `t_ms` never decreases; −40 to 150 °C.
- `cylinders` (optional, default 4): used only for the RPM estimate.
- `model_verdict` (optional): the rig's engine-anomaly verdict (`upclose-anomaly-verdict/1`; fields in the [rig README](../upclose-rig/README.md#verdict-upclose-anomaly-verdict1)). It is checked for internal consistency before it is accepted.

**Summary:**
- Dominant vibration frequency, an RPM estimate, roughness, and up to 20 irregular windows marked unverified.
- Temperature at the start and how much it rose, plus whether the engine was already warm.
- Too little data or no clear peak gives `not_assessed` with a reason.

### `obd_snapshot`

```json
{
  "vin": "SYNTHETC00000UPCA",
  "mil_on": true,
  "dtcs": [{"code": "P0301", "status": "stored"}],
  "readiness": {"misfire": "complete", "catalyst": "incomplete", "evap": "incomplete"},
  "distance_since_codes_cleared_km": 12,
  "warmups_since_codes_cleared": 1,
  "pids": {"rpm": 780.0, "coolant_c": 24.0}
}
```

- `vin` (optional): `^[A-HJ-NPR-Z0-9]{17}$`.
- `dtcs`: up to 64. Codes match `^[PCBU][0-3][0-9A-F]{3}$`; status is `stored`, `pending` or `permanent`.
- `readiness`: up to 16 monitors, each `complete`, `incomplete` or `unsupported`.
- `pids` (optional): up to 32 finite values.

**Summary:**
- Trouble codes, check-engine light, and incomplete monitors.
- **Codes recently cleared**: few kilometres or warm-ups since the last clear, or several monitors incomplete.
- Cold start, judged from the coolant temperature.

## In the report

- The report freezes each source's summary and data hash. Raw samples stay in the database, tied to the report by that hash.
- **Full report:** every check in full, each with a provenance badge (Simulated, Replayed or Live) and a note that values are uncalibrated estimates.
- **Free view:** only whether each check ran and how many flags it raised. No values, codes, VIN or traces.
- Deleting an inspection deletes its sources.

Summaries are computed by deterministic code with no network access. Thresholds come from a versioned config file.
