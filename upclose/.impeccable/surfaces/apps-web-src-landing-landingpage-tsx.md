---
version: 1
slug: "apps-web-src-landing-landingpage-tsx"
primary_target: "apps/web/src/landing/LandingPage.tsx"
related_targets: ["apps/web/src/sample"]
---

Scope: marketing landing (/) and public sample report (/sample/). Visitor mode: Persuade (landing), Read (sample report).
Audience: a private used-car buyer, phone in hand at the car; reviewers watching the demo.
Action: see the sample report; start an inspection when the app is live, or view the code on GitHub until then.
Constraints:
- Static hosting (Cloudflare Pages).
- No API calls, storage or tracking on the landing page.
- The sample report renders from a PaidReport JSON fixture plus one frame image per finding, so a real generated report can replace it.
- The single "Sample report" label is the only provenance note; legal text lives in the footer.

## Direction contract
THESIS: Show the evidence, not a promise. Real damage on one real vehicle, magnified on the page. This refuses the category-default glossy car marketplace hero and the AI-default cream/serif and neon-on-black looks.
OWN-WORLD:
- Cool grey ground #f3f4f5, white surfaces, graphite ink #111315.
- One orange accent #ff5a1f with ink text on orange, for actions, timecodes and evidence rings.
- Red only for serious findings; hatched stripes for anything not checked.
- Barlow Semi Condensed 800 for display, Barlow for body, and the lowercase "upclose" wordmark in Manrope with an orange dot.
- Evidence prints: frame, orange ring, timecode stamp.
STORY: The visitor sees rust they would have missed, understands that UpClose finds it frame by frame and is honest about what it couldn't check, then opens a full sample report.
FIRST VIEWPORT: The headline "Good find. Look closer." at the left, very large. The lede, primary action and price line at the right. Below, a full-width photo of the van at 16:9 with an orange-ringed magnifying loupe parked on the rust; the loupe follows the cursor on desktop. Then a legend of three findings with timecodes.
FORM: Category-standard structure carrying evidence-file details. This was the user's blend of the safer-round "Evidence File" card and the canon card. seed:cf54a445f34a0924
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
