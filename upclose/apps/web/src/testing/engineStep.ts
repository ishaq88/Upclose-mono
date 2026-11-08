import { fireEvent, screen } from '@testing-library/react';

/** The engine check is its own step: tap its item in the zone list before using the kits, the audio upload or the walkthrough. */
export async function openEngineStep(): Promise<void> {
  fireEvent.click(await screen.findByRole('button', { name: /^Engine check/ }));
}
