import type { KitId } from './kits';

export const LEARNING_SECONDS = 20;

export interface EngineStep {
  id: string;
  /** Voice-guide catalog line spoken when the step starts. */
  lineId: string;
  title: string;
  /** Local caption, shown when the catalog caption is not available. */
  text: string;
  /** The capture kit whose status this step watches. */
  kit: KitId;
  /** True for the timed learning step. */
  countdown?: boolean;
}

/** The engine check, in order. Line ids are the fixed catalog ids; the captions mirror the catalog text. */
export const ENGINE_STEPS: readonly EngineStep[] = [
  { id: 'pod_on', lineId: 'engine_pod_on', title: 'Pod on, engine off', text: 'Switch the pod on while the engine is off.', kit: 'rig_telemetry' },
  { id: 'start', lineId: 'engine_start', title: 'Start the engine', text: 'Now start the engine.', kit: 'rig_telemetry' },
  { id: 'learning', lineId: 'engine_hands_off', title: 'Hands off, learning', text: 'Hands off. The pod is learning for twenty seconds.', kit: 'rig_telemetry', countdown: true },
  { id: 'baseline', lineId: 'engine_baseline_learned', title: 'Baseline learned', text: 'Baseline learned.', kit: 'rig_telemetry' },
  { id: 'rev', lineId: 'engine_gentle_rev', title: 'One gentle rev', text: 'Give the engine one gentle rev.', kit: 'rig_telemetry' },
  { id: 'done', lineId: 'engine_done', title: 'Done, engine off', text: 'Done. Switch the engine off before removing the pod.', kit: 'rig_telemetry' },
  { id: 'obd', lineId: 'obd_plug_in', title: 'OBD reader', text: 'Plug the reader into the diagnostic port and wait.', kit: 'obd_snapshot' },
  { id: 'audio', lineId: 'engine_audio', title: 'Engine audio', text: 'Hold the phone near the engine for fifteen seconds.', kit: 'engine_audio' },
];

export const ENGINE_FINISH_LINE = { lineId: 'finish', text: 'That is everything. You can finish the inspection now.' };
