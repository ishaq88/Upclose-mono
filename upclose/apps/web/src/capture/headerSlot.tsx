import { createContext, useContext, useEffect, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { AppHeader } from '../ui/AppHeader';
import { Button } from '../ui/Button';
import { StatusChip } from '../ui/StatusChip';

/** What the capture page puts in the app header: the progress chip, and a Finish action that opens the wrap-up view. */
export interface CaptureHeader {
  filmed: number;
  total: number;
  checking: number;
  onFinish: (() => void) | null;
}

const SetValue = createContext<((value: CaptureHeader | null) => void) | null>(null);
const CurrentValue = createContext<CaptureHeader | null>(null);

export function HeaderSlotProvider({ children }: { children: ReactNode }): ReactElement {
  const [value, setValue] = useState<CaptureHeader | null>(null);
  return <SetValue.Provider value={setValue}><CurrentValue.Provider value={value}>{children}</CurrentValue.Provider></SetValue.Provider>;
}

/** The app header, filled from the capture page when one is mounted. Outside a provider it is the plain header. */
export function ConnectedHeader(): ReactElement {
  const value = useContext(CurrentValue);
  if (!value) return <AppHeader />;
  const status = <StatusChip filmed={value.filmed} total={value.total} checking={value.checking} />;
  const actions = value.onFinish ? <Button variant="text" size="sm" onClick={value.onFinish}>Finish</Button> : undefined;
  return <AppHeader status={status} actions={actions} />;
}

/** Called by the capture page. The handler is read through a ref, so it may change every render without re-publishing. */
export function useCaptureHeader(filmed: number, total: number, checking: number, onFinish: (() => void) | null): void {
  const set = useContext(SetValue);
  const handler = useRef(onFinish);
  handler.current = onFinish;
  const hasFinish = onFinish !== null;
  useEffect(() => {
    set?.({ filmed, total, checking, onFinish: hasFinish ? () => handler.current?.() : null });
  }, [set, filmed, total, checking, hasFinish]);
  useEffect(() => () => set?.(null), [set]);
}
