import type { components } from './generated/contracts';

type Schemas = components['schemas'];
type FreeReport = Schemas['FreeReport'];
type PaidReport = Schemas['PaidReport'];
type Report = FreeReport | PaidReport;
type Assert<T extends true> = T;
type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends
  (<T>() => T extends B ? 1 : 2) ? true : false;

// Compile-time checks: these files are included by typecheck, never by the app.
export type FreeHasNoPaidFields = Assert<Equal<
  Extract<keyof FreeReport, 'scores' | 'headline' | 'repairs' | 'claims' | 'narration'>,
  never
>>;
export type FreeScoreIsNumeric = Assert<Equal<FreeReport['part1_score'], number | null>>;
export type PaidScoreIsNumeric = Assert<Equal<Schemas['ScoreResult']['headline'], number | null>>;
export type FreeFindingIsPartOne = Assert<Equal<Schemas['FreeFinding']['zone'], 'driver_side'>>;

type StorageKeys = 'frame_key' | 'frame_keys' | 'evidence_frame_keys' | 'mask_key';
type DeepStorageKeys<T> = T extends readonly (infer Item)[] ? DeepStorageKeys<Item>
  : T extends object ? {
    [Key in keyof T]: Key extends StorageKeys ? Key : DeepStorageKeys<T[Key]>
  }[keyof T] : never;

export type FreeFindingHasNoStorageKeys = Assert<Equal<
  DeepStorageKeys<FreeReport['findings'][number]>, never
>>;
export type PaidFindingHasNoStorageKeys = Assert<Equal<
  DeepStorageKeys<PaidReport['findings'][number]>, never
>>;
export type PublicClaimsHaveNoStorageKeys = Assert<Equal<
  DeepStorageKeys<PaidReport['claims'][number]>, never
>>;
export type PublicClaimsRetainFindingReferences = Assert<Equal<
  PaidReport['claims'][number]['finding_ids'], string[]
>>;
export type PublicFindingSeverityIsRequired = Assert<Equal<
  Schemas['PublicFinding']['severity'], number
>>;
export type PublicFindingConfidenceIsExplicit = Assert<Equal<
  Schemas['PublicFinding']['confidence'], number | null
>>;
export type ScoringIncludesMultipleRuns = Assert<Equal<Schemas['ScoringInput']['run_ids'], string[]>>;
export type ScoringHasNoSingleRun = Assert<Equal<Extract<keyof Schemas['ScoringInput'], 'run_id'>, never>>;

export function assertReportNarrowing(report: Report): void {
  if (report.scope === 'free') {
    const free: FreeReport = report;
    void free.part1_score;
    // @ts-expect-error Paid scoring cannot be read after narrowing to free.
    void report.scores;
    // @ts-expect-error Paid repair details cannot be read after narrowing to free.
    void report.repairs;
    // @ts-expect-error Paid claims cannot be read after narrowing to free.
    void report.claims;
    // @ts-expect-error Paid narration cannot be read after narrowing to free.
    void report.narration;
  } else {
    const paid: PaidReport = report;
    void paid.scores.headline;
    void paid.repairs;
    void paid.claims;
    void paid.narration;
  }
}

// @ts-expect-error Historical misspelling is not a canonical zone.
export const invalidZone: Schemas['Zone'] = 'front_rear';
// @ts-expect-error Provider-invented component identities are not accepted.
export const invalidComponent: Schemas['ComponentId'] = 'invented_component';
