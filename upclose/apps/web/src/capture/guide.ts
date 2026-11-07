import type { components } from '../generated/contracts';

type Zone = components['schemas']['Zone'];

export interface GuideStep {
  partNo: number;
  zone: Zone;
  title: string;
  /** One-sentence goal in plain language. */
  goal: string;
  /** Short ordered instructions. */
  steps: readonly string[];
  /** Suggested pace, shown as guidance only (never enforced). */
  targetSeconds: number;
  /** Honest note when a zone is recorded but not assessed in this prototype. */
  prototypeNote?: string;
}

/**
 * Canonical eight parts, in order. Part numbers and zones mirror the backend contract (PART_ZONES).
 * Guidance never asks anyone to put any part of their body under a vehicle (contract section 5).
 */
export const GUIDE_STEPS: readonly GuideStep[] = [
  {
    partNo: 1, zone: 'driver_side', title: 'Driver side', targetSeconds: 20,
    goal: 'Film the whole driver side, with extra time low on the door sills and wheel arches.',
    steps: [
      'Stand about 1.5 m (5 ft) from the car with your phone held sideways.',
      'Start at the rear corner and walk slowly toward the front.',
      'Tip the phone down for a moment at the bottom edge of the doors and over each wheel arch.',
      'Keep a steady pace: roughly 20 seconds in total.',
    ],
  },
  {
    partNo: 2, zone: 'passenger_side', title: 'Passenger side', targetSeconds: 20,
    goal: 'Repeat the same slow pass along the passenger side.',
    steps: [
      'Start at the front corner and walk toward the rear.',
      'Pause briefly on the lower door edges and wheel arches.',
      'Aim for about 20 seconds.',
    ],
  },
  {
    partNo: 3, zone: 'front_rear_body', title: 'Front and rear, with glass', targetSeconds: 30,
    goal: 'Show both ends of the car, including the windshield and rear window.',
    steps: [
      'Film the front bumper, hood and windshield from a little way back.',
      'Walk around to the rear and film the trunk, bumper and rear window.',
      'Move slowly so glass reflections do not hide cracks. Aim for about 30 seconds.',
    ],
  },
  {
    partNo: 4, zone: 'tires', title: 'Tires', targetSeconds: 20,
    goal: 'Show the tread and sidewall of each tire.',
    steps: [
      'Bend to a comfortable height beside each tire. Do not reach under the car.',
      'Hold on each tire for 3 to 5 seconds, showing the tread face and sidewall.',
      'Do all four tires in one clip, about 20 seconds.',
    ],
  },
  {
    partNo: 5, zone: 'dash_electrical', title: 'Dashboard', targetSeconds: 10,
    goal: 'Show the instrument panel clearly.',
    steps: [
      'With the car safely parked, hold the phone steady on the dash for about 10 seconds.',
      'Reduce glare by shading the screen with your body.',
    ],
    prototypeNote: 'Dashboard footage is kept with your session but is not scored in this prototype.',
  },
  {
    partNo: 6, zone: 'engine_bay', title: 'Engine bay', targetSeconds: 20,
    goal: 'Show the engine bay and anything dripping or stained.',
    steps: [
      'Only with the engine off and cool, open the hood and secure it.',
      'Pan slowly across the engine bay, then down the sides. Do not touch hot or moving parts.',
      'Aim for about 20 seconds.',
    ],
  },
  {
    partNo: 7, zone: 'underbody', title: 'Underbody, low sweep', targetSeconds: 20,
    goal: 'Show the lower edge and ground clearance from a standing position.',
    steps: [
      'Stay standing and keep your whole body outside the car at all times.',
      'Hold the phone low, near knee height, and sweep it slowly along the side of the car so it looks under the edge.',
      'Do not crawl, kneel under, lie down, or reach beneath the vehicle. Never go near a car that is on a jack or stands.',
      'If you cannot do this safely and comfortably, skip this zone.',
    ],
  },
  {
    partNo: 8, zone: 'interior', title: 'Interior', targetSeconds: 15,
    goal: 'Show the seats, carpets and headliner.',
    steps: [
      'Open a door and film the front seats, then the back seats and the floor.',
      'Finish with a slow look at the roof lining. About 15 seconds.',
    ],
    prototypeNote: 'Interior footage is kept with your session but is not assessed in this prototype.',
  },
];

export function guideFor(partNo: number): GuideStep | undefined {
  return GUIDE_STEPS.find((step) => step.partNo === partNo);
}
