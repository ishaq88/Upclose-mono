# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Primary: a private buyer of a cheap-to-mid-priced used car, standing at a seller's driveway or a small lot with a phone in hand, partway through a walkaround. They want to see real visible condition (rust, dents, scratches, cracked glass, worn tires, leaks) before handing over money. Phone-first, outdoors, often one-handed, often in bright daylight.

Secondary: reviewers watching a 3-minute demo video.

## Product Purpose
UpClose screens used-car condition from phone video. The buyer records either one walkaround video (up to 180 s, 200 MiB) or guided clips for eight zones. The backend finds the car, then looks for damage only on the car, and returns a report: a zone-by-zone visual-condition index, findings with evidence frames, and an honest record of which zones were not assessed. Success means the buyer leaves with specific questions for the seller and a mechanic, not a false all-clear.

## Positioning
Chatbots describe a photo. UpClose measures: SAM 3.1 pixel masks (car first, then damage prompts on the masked car only), a fixed deterministic scoring rubric, persisted evidence for every finding, and coverage honesty. Missing, blurry, or skipped footage is never a clean score.

## Operating Context
- Capture modes: One video, or Guided clips for 8 zones (driver side, passenger side, front/rear with glass, tires, and four more). The free result covers the driver side (Part 1) only.
- Live progress while processing. The buyer chooses when to finish capture; finishing marks unfilmed zones as skipped.
- Report at `/r/{slug}`: free scope versus paid scope. Paid unlocks the full report for $19 through Stripe Checkout in test mode.
- Accounts (new API, no UI yet): register, log in, log out, change password, claim an inspection session into an account, list my inspections.
- The owner's session is tied to the browser tab unless claimed into an account.
- Delete session media at any time; deletion is verified.

## Capabilities and Constraints
- Stack: React 19 + Vite + TypeScript + Tailwind v4 (`apps/web`); a separate landing build (`vite.landing.config.ts`); FastAPI + Jinja for `/r/{slug}`; TS types are generated from Pydantic contracts.
- Results carry a provenance label: synthetic (fixture), cached, or live. Synthetic results must say so wherever shown.
- Scores are a "visual-condition index" out of 100, shown to one decimal and never rounded up. The headline is null when fewer than 3 zones were assessed. Exact copy for an assessed zone with no findings: "No visible findings detected in assessed footage" (never "damage free").
- Model confidence is uncalibrated; no accuracy claims anywhere.
- Free views must never expose paid evidence.
- Guidance never asks anyone to go under a vehicle.

## Brand Commitments
- Name: UpClose.
- Keep the "Look closer" line (current: "Good find. Look closer.").
- Everything else (mark, photo, colours, type) is replaceable. Do not use a black-and-purple theme.

## Evidence on Hand
- No real customers, testimonials, or accuracy evaluation exist. Do not invent them.
- Test counts (961 backend, 97 web, as of 2026-11-04) are real engineering facts.
- Sample report content must be labeled synthetic or illustrative.

## Product Principles
1. Unknown is never clean: coverage gaps are shown as plainly as findings.
2. Evidence before verdict: every finding points to a frame.
3. Built for the walkaround: thumb-reachable, legible outdoors, interruptible.
4. Honest provenance: synthetic, cached, and live are always labeled.

## Accessibility & Inclusion
WCAG 2.2 AA. Outdoor, sunlit phone use demands high contrast and large touch targets (at least 44 px). Respect reduced motion.
