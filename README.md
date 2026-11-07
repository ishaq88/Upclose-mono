# UpClose: used-car inspection with capture-kit plugins

UB Hacking Fall 2026 submission.

UpClose turns a phone walkaround video into a private, evidence-linked condition report for a used car. At UB Hacking we turned it into an **inspection platform with capture-kit plugins**:
- a **sensor pod** (Seeed Grove Beginner Kit) that senses engine vibration, sound, and temperature;
- an **OBD reader**;
- an **engine-anomaly model** that alerts the inspector on the pod itself;
- a **3D digital twin** for testing the vision pipeline against known damage.

Everything a plugin captures is attached to the inspection before the report is generated and appears in the report as "Additional checks".

## What existed before the event, and what we built at it

| Part | Status |
|---|---|
| `upclose/`: capture web app, API, vehicle and damage segmentation pipeline, scoring, report, payments | **Built before UB Hacking.** Imported in the first commit of this repo as a snapshot of the 2026-11-04 state, with no earlier history. |
| `upclose-rig/`: pod firmware, serial bridge, simulator, emulated OBD adapter, engine-anomaly model, live pod alerts, digital twin | **Built at UB Hacking** (2026-11-07/08) |
| Changes inside `upclose/` after the first commit (evidence sources, "Additional checks" report section, advisory, voice, capture-kits panel, and others) | **Built at UB Hacking.** See the commits after the import. |

## Simulated and synthetic data disclosure

We had no car and no physical OBD adapter at the event. Wherever an input is simulated, emulated, rendered, or an internet sample, the product labels it `synthetic`, and so do this README and the demo:
- **Engine vibration:** the simulator, or the Grove pod on a bench stand-in.
- **OBD:** an emulated ELM327 adapter, read by the real python-OBD library, with a synthetic VIN.
- **Engine audio:** CC0 internet clips.
- **Walkaround video of the twin:** a 3D render.

The anomaly model's evaluation numbers come from simulator data only. They are **not** real-car accuracy.

## Layout

```
upclose/        platform (FastAPI + PostgreSQL + React); see upclose/README.md
upclose-rig/    hardware + AI plugins; see upclose-rig/README.md
docs/           the evidence-source contract shared by both
```
