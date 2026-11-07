import type { components } from '../generated/contracts';
import { GUIDE_STEPS } from '../capture/guide';

type Schemas = components['schemas'];
export type Zone = Schemas['Zone'];
export type Concept = Schemas['Concept'];
export type CoverageState = Schemas['CoverageState'];

export const CONCEPT_LABEL: Record<Concept, string> = {
  rust: 'Rust',
  paint_bubbling: 'Paint bubbling',
  dent: 'Dent',
  scratch: 'Scratch',
  glass_crack: 'Glass crack',
  tire_wear: 'Tire wear',
  fluid_leak: 'Fluid leak',
};

export const COVERAGE_LABEL: Record<CoverageState, string> = {
  assessed: 'Assessed',
  partial: 'Partly assessed',
  not_assessed: 'Not assessed',
};

export function zoneTitle(zone: Zone): string {
  return GUIDE_STEPS.find((step) => step.zone === zone)?.title ?? zone;
}

export function conceptLabel(concept: string): string {
  return (CONCEPT_LABEL as Record<string, string>)[concept] ?? concept;
}
