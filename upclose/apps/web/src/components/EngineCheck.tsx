import { useEffect, useRef, useState, type ReactElement } from 'react';
import { ENGINE_FINISH_LINE, ENGINE_STEPS, LEARNING_SECONDS } from '../capture/engineCheck';
import { PROVENANCE_LABEL, type KitRow } from '../capture/kits';
import { useVoiceGuide } from '../capture/voiceGuide';

interface Props {
  rows: readonly KitRow[];
  capturing: boolean;
  learningSeconds?: number;
}

/**
 * Step-by-step engine check. Each step shows its caption and, when the voice guide is on, speaks it from the tap that
 * opened the step. The 20 s learning step is a guide timer: the pod's real state is whatever Capture kits reports.
 */
export function EngineCheck({ rows, capturing, learningSeconds = LEARNING_SECONDS }: Props): ReactElement {
  const voice = useVoiceGuide();
  const voiceRef = useRef(voice);
  voiceRef.current = voice; // the countdown callback must see the current on/off choice
  const [index, setIndex] = useState(-1);
  const [remaining, setRemaining] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const finished = index >= ENGINE_STEPS.length;
  const step = index >= 0 && !finished ? ENGINE_STEPS[index]! : null;

  const clearTimer = (): void => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
  };
  useEffect(() => clearTimer, []);

  const go = (next: number): void => {
    clearTimer();
    setRemaining(null);
    setIndex(next);
    if (next >= ENGINE_STEPS.length) voiceRef.current.play(ENGINE_FINISH_LINE.lineId, ENGINE_FINISH_LINE.text);
    else if (next >= 0) voiceRef.current.play(ENGINE_STEPS[next]!.lineId, ENGINE_STEPS[next]!.text);
  };

  const startLearning = (): void => {
    let left = learningSeconds;
    setRemaining(left);
    timer.current = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        // Moves on without a tap; audio may be blocked by the browser, and the caption still shows.
        go(ENGINE_STEPS.findIndex((candidate) => candidate.id === 'learning') + 1);
      } else {
        setRemaining(left);
      }
    }, 1000);
  };

  const status = (kitId: string): ReactElement | null => {
    const row = rows.find((candidate) => candidate.id === kitId);
    if (!row) return null;
    return (
      <p className="hint" role="status" data-testid={`engine-kit-${row.id}`}>
        {row.label}: {row.attached ? `attached${row.provenance ? ` (${PROVENANCE_LABEL[row.provenance]})` : ''}` : 'waiting for it to attach'}
      </p>
    );
  };

  return (
    <section id="engine-check" aria-labelledby="engine-heading" className="card engine-check" data-testid="engine-check">
      <h2 id="engine-heading">Engine check</h2>
      <p className="hint">Follow the steps with the sensor pod and OBD reader. Each step is shown as text and can also be spoken.</p>
      <ol className="engine-steps">
        {ENGINE_STEPS.map((candidate, position) => (
          <li key={candidate.id} aria-current={position === index ? 'step' : undefined} className={position === index ? 'engine-step engine-step-current' : 'engine-step'}>
            <span className="engine-step-title">{candidate.title}</span>
            {position < index || finished ? <span className="tag tag-quiet">Done</span> : null}
          </li>
        ))}
      </ol>
      {index < 0 && (
        <button type="button" className="btn btn-sm" disabled={!capturing} data-testid="engine-check-start" onClick={() => go(0)}>Start engine check</button>
      )}
      {step && (
        <div className="stack" data-testid="engine-current">
          <p className="engine-caption" data-testid="engine-caption">{voice.caption(step.lineId) ?? step.text}</p>
          {status(step.kit)}
          {step.countdown && remaining !== null && <p role="timer" className="num" data-testid="engine-countdown">{remaining} s of {learningSeconds} s</p>}
          <div className="cap-actions">
            {step.countdown && remaining === null && <button type="button" className="btn btn-sm" data-testid="engine-check-learn" onClick={startLearning}>Start {learningSeconds} s learning</button>}
            {!step.countdown && (
              <button type="button" className="btn btn-sm" data-testid="engine-check-next" onClick={() => go(index + 1)}>{index === ENGINE_STEPS.length - 1 ? 'Finish engine check' : 'Next step'}</button>
            )}
            {voice.enabled && <button type="button" className="btn btn-ghost btn-sm" onClick={() => voice.play(step.lineId, step.text)}>Hear this step again</button>}
          </div>
          {step.id === 'audio' && <p className="hint">Add the recording under Engine audio in Capture kits.</p>}
        </div>
      )}
      {finished && <p className="engine-caption" role="status" data-testid="engine-finished">{voice.caption(ENGINE_FINISH_LINE.lineId) ?? ENGINE_FINISH_LINE.text}</p>}
    </section>
  );
}
