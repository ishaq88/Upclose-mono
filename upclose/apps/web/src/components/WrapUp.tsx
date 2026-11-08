import type { ReactElement, ReactNode } from 'react';
import type { PartSummary } from '../api/client';
import { GUIDE_STEPS, guideFor } from '../capture/guide';
import { GuideViewer } from '../capture/GuideViewer';
import type { KitRow } from '../capture/kits';
import { OPTIONAL_PARTS } from '../capture/zones';
import type { UiError } from '../errors';
import { ActionBar } from '../ui/ActionBar';
import { Button } from '../ui/Button';
import { Card } from '../ui/Card';
import { Notice } from '../ui/Notice';
import { Pill, type PillTone } from '../ui/Pill';
import { ErrorNotice } from './PartCard';

export interface WrapUpProps {
  /** Parts in display order. */
  parts: readonly PartSummary[];
  /** Part numbers with footage (uploaded, processing or processed): washed on the car. */
  done: readonly number[];
  /** Kits not attached; their Mechanical checks will read "not checked". */
  unattached: readonly KitRow[];
  /** Why Finish is blocked, in a few words for the button ("Waiting for 1 clip to start"), or null. */
  blockedLabel: string | null;
  /** The longer plain-language reason, shown under the buttons. */
  blockedDetail: string | null;
  blockedTestId?: string;
  /** Zones with no footage yet: they are marked skipped when the inspection is finished. */
  notFilmedCount: number;
  finishing: boolean;
  finishError: UiError | null;
  onDismissError: () => void;
  onFinish: () => void;
  onKeepFilming: () => void;
  onOpenEngine: () => void;
  /** The side column at 1100 px and up: processing and the unlock card. */
  side: ReactNode;
}

interface Coverage { label: string; tone: PillTone }

/** What a zone will read as in the report. Footage is "Filmed", never "clean"; nothing unknown is called fine. */
export function coverageOf(part: PartSummary): Coverage {
  switch (part.state) {
    case 'uploaded': case 'processing': case 'processed': return { label: 'Filmed', tone: 'filmed' };
    case 'skipped': return { label: 'Skipped', tone: 'not' };
    case 'failed': return { label: 'Could not be checked', tone: 'risk' };
    case 'uploading': return { label: 'Still uploading', tone: 'warn' };
    case 'pending': return OPTIONAL_PARTS.has(part.part_no)
      ? { label: 'Optional, not filmed', tone: 'optional' }
      : { label: 'Not filmed, will be skipped', tone: 'warn' };
  }
}

/** The wrap-up view: the whole van with the filmed zones tinted, eight coverage rows, and the one irreversible action. */
export function WrapUp(props: WrapUpProps): ReactElement {
  const { parts, done, unattached, blockedLabel, blockedDetail, finishing } = props;
  return (
    <section aria-labelledby="wrapup-heading" className="wrapup" data-testid="wrap-up">
      <div className="wrapup-main">
        <header className="wrapup-head">
          <h2 id="wrapup-heading" className="page-title">Ready to build the report?</h2>
          <p className="muted">You can't add clips after this. Checking carries on in the background.</p>
          {props.notFilmedCount > 0 && <p className="muted">{props.notFilmedCount} zone{props.notFilmedCount === 1 ? '' : 's'} not yet filmed will be marked skipped.</p>}
        </header>
        <div className="wrapup-car"><GuideViewer guide={GUIDE_STEPS[0]!} done={done} overview /></div>
        <ul className="wrapup-rows" aria-label="Coverage by zone">
          {parts.map((part) => {
            const coverage = coverageOf(part);
            return (
              <li key={part.id} data-testid={`wrap-row-${part.part_no}`}>
                <span className="wrapup-name">{guideFor(part.part_no)?.title ?? `Step ${part.part_no}`}</span>
                <Pill tone={coverage.tone}>{coverage.label}</Pill>
              </li>
            );
          })}
        </ul>
        {unattached.length > 0 && (
          <Notice tone="warn" title="Mechanical will read not checked"
            action={<Button variant="text" size="sm" onClick={props.onOpenEngine}>Open the engine check</Button>}>
            <p data-testid="missing-kits" role="status">
              <span className="lead-in">Still waiting: </span>{unattached.map((row) => row.label).join(', ')}. They will show as not checked in the report.
            </p>
          </Notice>
        )}
        {blockedDetail && <p className="finish-state" role="status" data-testid={props.blockedTestId}>{blockedDetail}</p>}
        {props.finishError && <ErrorNotice error={props.finishError} onDismiss={props.onDismissError} />}
        <ActionBar className="wrapup-actions" label="Finish or keep filming">
          {blockedLabel
            ? <Button variant="dark" disabled>{blockedLabel}</Button>
            : <Button variant="dark" disabled={finishing} onClick={props.onFinish}>Finish and build report</Button>}
          <Button variant="secondary" onClick={props.onKeepFilming}>Keep filming</Button>
        </ActionBar>
      </div>
      <aside className="wrapup-side" aria-label="Processing and report">{props.side}</aside>
    </section>
  );
}

export function UnlockTeaser(): ReactElement {
  return (
    <Card tone="sunk" padding="md" className="stack">
      <h3 className="sub-title">After you finish</h3>
      <p className="hint">The Driver side result is free. The full report is $19, unlocked only after the payment is verified.</p>
    </Card>
  );
}
