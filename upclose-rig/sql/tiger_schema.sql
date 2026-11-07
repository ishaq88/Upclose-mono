-- Tiger Data store for Grove pod telemetry.
--
-- Idempotent: safe to apply more than once (tiger-init runs this file).
-- Raw pod samples live in the `pod_samples` hypertable, anomaly verdicts in
-- `pod_verdicts`, and the `pod_samples_1s` continuous aggregate keeps
-- per-second rollups (vibration RMS, sound level) for fast run summaries.

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TABLE IF NOT EXISTS pod_runs (
  run_id     TEXT        PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL,
  source     TEXT        NOT NULL CHECK (source IN ('pod', 'simulator', 'replay')),
  provenance TEXT        NOT NULL CHECK (provenance IN ('real', 'synthetic')),
  notes      TEXT        NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pod_samples (
  run_id TEXT        NOT NULL REFERENCES pod_runs (run_id) ON DELETE CASCADE,
  ts     TIMESTAMPTZ NOT NULL,
  kind   CHAR(1)     NOT NULL CHECK (kind IN ('A', 'S', 'T')),
  ax_mg  INTEGER,
  ay_mg  INTEGER,
  az_mg  INTEGER,
  sound  INTEGER     CHECK (sound IS NULL OR (sound >= 0 AND sound <= 1023)),
  temp_c DOUBLE PRECISION,
  rh     DOUBLE PRECISION
);

SELECT create_hypertable('pod_samples', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS pod_samples_run_ts ON pod_samples (run_id, ts);

CREATE TABLE IF NOT EXISTS pod_verdicts (
  run_id       TEXT        NOT NULL REFERENCES pod_runs (run_id) ON DELETE CASCADE,
  ts           TIMESTAMPTZ NOT NULL,
  state        TEXT        NOT NULL CHECK (state IN ('anomaly_suspected', 'no_anomaly_detected', 'not_assessed')),
  score        DOUBLE PRECISION,
  top_features JSONB       NOT NULL DEFAULT '[]'::jsonb
);

CREATE MATERIALIZED VIEW IF NOT EXISTS pod_samples_1s
WITH (timescaledb.continuous) AS
SELECT
  time_bucket(INTERVAL '1 second', ts) AS bucket,
  run_id,
  COUNT(*) FILTER (WHERE kind = 'A') AS n_accel,
  SQRT(AVG((ax_mg::double precision * ax_mg::double precision
            + ay_mg::double precision * ay_mg::double precision
            + az_mg::double precision * az_mg::double precision))
       FILTER (WHERE kind = 'A')) AS accel_rms,
  AVG(sound::double precision) FILTER (WHERE kind = 'S') AS sound_mean,
  MAX(sound) FILTER (WHERE kind = 'S') AS sound_max
FROM pod_samples
GROUP BY bucket, run_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('pod_samples_1s',
  start_offset => INTERVAL '1 hour',
  end_offset => INTERVAL '10 seconds',
  schedule_interval => INTERVAL '1 minute',
  if_not_exists => TRUE);
