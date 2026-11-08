import { expect, type Page } from '@playwright/test';
import { ZONES } from './env.js';

export const ZONE_TITLE = Object.fromEntries(ZONES.map((z) => [z.part, z.title])) as Record<number, string>;

/** Consent screen -> guided capture screen. */
export async function startGuidedInspection(page: Page): Promise<void> {
  await page.goto('/');
  await page.locator('label.tile', { hasText: 'Guided clips' }).click();
  await page.locator('label.check', { hasText: 'I understand and agree' }).click();
  await page.getByRole('button', { name: 'Continue with guided clips' }).click();
  await expect(page.getByRole('navigation', { name: 'Zones' }), 'step "start inspection": zone list did not appear').toBeVisible({ timeout: 30_000 });
}

export async function openZone(page: Page, part: number): Promise<void> {
  await page.getByRole('navigation', { name: 'Zones' }).getByRole('button', { name: new RegExp(escape(ZONE_TITLE[part]!)) }).click();
  await expect(page.locator(`article.guide[data-part="${part}"]`)).toBeVisible();
}

/** Server-side state label of a zone in the zone list. */
export function zoneState(page: Page, part: number) {
  return page.getByTestId(`state-${part}`);
}

/** Pick a clip through the zone's upload input, then confirm the review step. */
export async function uploadZoneClip(page: Page, part: number, file: string): Promise<void> {
  await openZone(page, part);
  const card = page.locator(`article.guide[data-part="${part}"]`);
  // The second file input is the plain upload; the first has capture="environment".
  await card.locator('input[type=file]:not([capture])').first().setInputFiles(file);
  await card.getByRole('button', { name: 'Upload this clip' }).click();
}

function escape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
