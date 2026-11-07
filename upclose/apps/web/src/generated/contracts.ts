// Generated from services/api/schema/openapi.json. Do not edit.
// Regenerate the Pydantic schema first, then run npm --prefix apps/web run generate:types.
export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AccelData */
        AccelData: {
            /** Rate Hz */
            rate_hz: number;
            /** X Mg */
            x_mg: number[];
            /** Y Mg */
            y_mg: number[];
            /** Z Mg */
            z_mg: number[];
        };
        /** AccountSessionsResponse */
        AccountSessionsResponse: {
            /** Sessions */
            sessions: components["schemas"]["AccountSessionSummary"][];
        };
        /**
         * AccountSessionSummary
         * @description Free-safe account listing row: no findings, costs, evidence, or tokens.
         */
        AccountSessionSummary: {
            /**
             * Capture Mode
             * @default guided
             * @enum {string}
             */
            capture_mode: "guided" | "single_video";
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Part State Counts */
            part_state_counts: {
                [key: string]: number;
            };
            /**
             * Purpose
             * @constant
             */
            purpose: "buyer";
            /**
             * Report Slug
             * @default null
             */
            report_slug: string | null;
            /** @default null */
            report_state: components["schemas"]["ReportState"] | null;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            state: components["schemas"]["SessionState"];
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /**
             * Vehicle Label
             * @default null
             */
            vehicle_label: string | null;
        };
        /** AccountSummary */
        AccountSummary: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Disabled
             * @default false
             */
            disabled: boolean;
            /** Email */
            email: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
        };
        /** Artifact */
        Artifact: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Key */
            key: string;
            privacy_state: components["schemas"]["PrivacyState"];
            /**
             * Revision Id
             * @default null
             */
            revision_id: string | null;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            /** Sha256 */
            sha256: string;
        };
        /**
         * AuthResponse
         * @description The plaintext token is returned exactly once, at issuance.
         */
        AuthResponse: {
            account: components["schemas"]["AccountSummary"];
            /**
             * Expires At
             * Format: date-time
             */
            expires_at: string;
            /** Token */
            token: string;
        };
        /** BoundingBox */
        BoundingBox: {
            /** H */
            h: number;
            /** W */
            w: number;
            /** X */
            x: number;
            /** Y */
            y: number;
        };
        /** CandidateFinding */
        CandidateFinding: {
            bbox: components["schemas"]["BoundingBox"];
            concept: components["schemas"]["Concept"];
            /** Confidence */
            confidence: number | null;
            /**
             * Confidence Label
             * @default Uncalibrated model confidence, not an accuracy probability
             * @constant
             */
            confidence_label: "Uncalibrated model confidence, not an accuracy probability";
            /** Evidence Available */
            evidence_available: boolean;
            /** Evidence Note */
            evidence_note: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Mapping Basis
             * @default null
             */
            mapping_basis: "user_declared" | null;
            /**
             * Source Timestamp Ms
             * @default null
             */
            source_timestamp_ms: number | null;
            zone: components["schemas"]["Zone"];
        };
        /** ChangePasswordRequest */
        ChangePasswordRequest: {
            /** Current Password */
            current_password: string;
            /** New Password */
            new_password: string;
        };
        /** CheckoutRequest */
        CheckoutRequest: {
            /**
             * Product
             * @constant
             */
            product: "report_unlock";
        };
        /** CheckoutResponse */
        CheckoutResponse: {
            /**
             * Checkout Url
             * @default null
             */
            checkout_url: string | null;
            /**
             * Unlocked
             * @default false
             */
            unlocked: boolean;
        };
        /**
         * ClaimGroup
         * @description Trusted revision evidence whitelist plus model claim results.
         */
        ClaimGroup: {
            /** Claims */
            claims: components["schemas"]["ClaimResult"][];
            /** Findings */
            findings: components["schemas"]["Finding"][];
        };
        /** ClaimResult */
        ClaimResult: {
            /** Claim Index */
            claim_index: number;
            /** Evidence Frame Keys */
            evidence_frame_keys: string[];
            /** Finding Ids */
            finding_ids: string[];
            /** Rationale */
            rationale: string;
            /** Text */
            text: string;
            verdict: components["schemas"]["ClaimVerdict"];
        };
        /** ClaimSessionRequest */
        ClaimSessionRequest: {
            /** Owner Token */
            owner_token: string;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
        };
        /** ClaimSessionResponse */
        ClaimSessionResponse: {
            /**
             * Account Id
             * Format: uuid
             */
            account_id: string;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
        };
        /**
         * ClaimVerdict
         * @enum {string}
         */
        ClaimVerdict: "supported" | "contradicted" | "not_visible";
        /**
         * ComponentId
         * @enum {string}
         */
        ComponentId: "driver_rocker" | "passenger_rocker" | "windshield" | "tire_fl" | "tire_fr" | "tire_rl" | "tire_rr" | "driver_front_door" | "driver_rear_door" | "passenger_front_door" | "passenger_rear_door" | "hood" | "roof" | "trunk" | "front_bumper" | "rear_bumper";
        /**
         * Concept
         * @enum {string}
         */
        Concept: "rust" | "paint_bubbling" | "dent" | "scratch" | "glass_crack" | "tire_wear" | "fluid_leak";
        /** ConceptRun */
        ConceptRun: {
            concept: components["schemas"]["Concept"];
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Part Id
             * Format: uuid
             */
            part_id: string;
            /** Prompt Id */
            prompt_id: string;
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            state: components["schemas"]["ConceptRunState"];
            zone: components["schemas"]["Zone"];
        };
        /**
         * ConceptRunState
         * @enum {string}
         */
        ConceptRunState: "succeeded" | "failed" | "unsupported" | "budget_exhausted";
        /** ContractModel */
        ContractModel: Record<string, never>;
        /** Coverage */
        Coverage: {
            concept: components["schemas"]["Concept"];
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            state: components["schemas"]["CoverageState"];
            zone: components["schemas"]["Zone"];
        };
        /**
         * CoverageState
         * @enum {string}
         */
        CoverageState: "assessed" | "partial" | "not_assessed";
        /** CreateSessionRequest */
        CreateSessionRequest: {
            /**
             * Capture Mode
             * @default guided
             * @enum {string}
             */
            capture_mode: "guided" | "single_video";
            /**
             * Consent
             * @constant
             */
            consent: true;
            /** Consent Version */
            consent_version: string;
            /**
             * Listing Text
             * @default null
             */
            listing_text: string | null;
            /**
             * Purpose
             * @constant
             */
            purpose: "buyer";
        };
        /** CreateSessionResponse */
        CreateSessionResponse: {
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            /** Session Token */
            session_token: string;
        };
        /** CreateUploadRequest */
        CreateUploadRequest: {
            /** Content Length */
            content_length: number;
            /**
             * Content Type
             * @enum {string}
             */
            content_type: "video/mp4" | "video/webm" | "video/quicktime";
            /** Part No */
            part_no: number;
        };
        /** CreateUploadResponse */
        CreateUploadResponse: {
            /** Chunk Count */
            chunk_count: number;
            /**
             * Chunk Size Bytes
             * @default 8388608
             * @constant
             */
            chunk_size_bytes: 8388608;
            /**
             * Part Id
             * Format: uuid
             */
            part_id: string;
            /**
             * Upload Id
             * Format: uuid
             */
            upload_id: string;
        };
        /** CreateVideoUploadRequest */
        CreateVideoUploadRequest: {
            /** Content Length */
            content_length: number;
            /**
             * Content Type
             * @enum {string}
             */
            content_type: "video/mp4" | "video/webm" | "video/quicktime";
            /**
             * Segments
             * @default []
             */
            segments: components["schemas"]["VideoRange"][];
        };
        /** CreateVideoUploadResponse */
        CreateVideoUploadResponse: {
            /** Chunk Count */
            chunk_count: number;
            /**
             * Chunk Size Bytes
             * @default 8388608
             * @constant
             */
            chunk_size_bytes: 8388608;
            /**
             * Upload Id
             * Format: uuid
             */
            upload_id: string;
        };
        /** DeleteMediaResponse */
        DeleteMediaResponse: {
            /**
             * State
             * @enum {string}
             */
            state: "deleting" | "deleted";
        };
        /** DtcEntry */
        DtcEntry: {
            /** Code */
            code: string;
            /**
             * Status
             * @enum {string}
             */
            status: "stored" | "pending" | "permanent";
        };
        /** EmptyRequest */
        EmptyRequest: Record<string, never>;
        /** EvidenceRef */
        EvidenceRef: {
            bbox: components["schemas"]["BoundingBox"];
            /** Frame Height */
            frame_height: number;
            /** Frame Index */
            frame_index: number;
            /** Frame Key */
            frame_key: string;
            /** Frame Width */
            frame_width: number;
            /**
             * Mask Key
             * @default null
             */
            mask_key: string | null;
        };
        /** FinalizeResponse */
        FinalizeResponse: {
            /** Report Slug */
            report_slug: string;
            /**
             * State
             * @default finalized
             * @constant
             */
            state: "finalized";
        };
        /** Finding */
        Finding: {
            /** @default null */
            component_id: components["schemas"]["ComponentId"] | null;
            concept: components["schemas"]["Concept"];
            /**
             * Concept Run Id
             * Format: uuid
             */
            concept_run_id: string;
            /**
             * Confidence
             * @default null
             */
            confidence: number | null;
            /**
             * Coverage Pct
             * @default null
             */
            coverage_pct: null;
            /**
             * Duplicate Of
             * @default null
             */
            duplicate_of: string | null;
            evidence: components["schemas"]["EvidenceRef"];
            /** Evidence Note */
            evidence_note: string;
            /**
             * Flags
             * @default []
             */
            flags: string[];
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Part Id
             * Format: uuid
             */
            part_id: string;
            /** Part No */
            part_no: number;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            /**
             * Severity
             * @default null
             */
            severity: number | null;
            status: components["schemas"]["FindingStatus"];
            /** @default null */
            subzone: components["schemas"]["Subzone"] | null;
            /** Track Id */
            track_id: string;
            zone: components["schemas"]["Zone"];
        };
        /**
         * FindingStatus
         * @enum {string}
         */
        FindingStatus: "candidate" | "verified" | "rejected" | "unverified";
        /** FixtureMetadata */
        FixtureMetadata: {
            /** Description */
            description: string;
            /** Provenance */
            provenance: string;
        };
        /** FreeFinding */
        FreeFinding: {
            bbox: components["schemas"]["BoundingBox"];
            component_id: components["schemas"]["ComponentId"] | null;
            concept: components["schemas"]["Concept"];
            /** Confidence */
            confidence: number | null;
            /**
             * Confidence Label
             * @default Uncalibrated model confidence, not an accuracy probability
             * @constant
             */
            confidence_label: "Uncalibrated model confidence, not an accuracy probability";
            /** Evidence Available */
            evidence_available: boolean;
            /** Evidence Note */
            evidence_note: string;
            /** Flags */
            flags: string[];
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Mapping Basis
             * @default null
             */
            mapping_basis: "user_declared" | null;
            /** Severity */
            severity: number;
            /**
             * Source Timestamp Ms
             * @default null
             */
            source_timestamp_ms: number | null;
            subzone: components["schemas"]["Subzone"] | null;
            /**
             * Zone
             * @default driver_side
             * @constant
             */
            zone: "driver_side";
        };
        /** FreeReport */
        FreeReport: {
            /**
             * Additional Checks
             * @default []
             */
            additional_checks: components["schemas"]["FreeSourceCheck"][];
            /** Coverage */
            coverage: components["schemas"]["PublicCoverage"][];
            /** Findings */
            findings: components["schemas"]["FreeFinding"][];
            /** Locked Zones */
            locked_zones: components["schemas"]["LockedZoneSummary"][];
            /** Part1 Score */
            part1_score: number | null;
            /** Part1 Summary */
            part1_summary: string;
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /**
             * Scope
             * @default free
             * @constant
             */
            scope: "free";
            /** Slug */
            slug: string;
            state: components["schemas"]["ReportState"];
            /**
             * Vehicle Label
             * @default null
             */
            vehicle_label: string | null;
        };
        /**
         * FreeSourceCheck
         * @description Free scope: only whether a kind was checked and how many flags it raised. No values.
         */
        FreeSourceCheck: {
            /** Checked */
            checked: boolean;
            /**
             * Flag Count
             * @default 0
             */
            flag_count: number;
            /**
             * Kind
             * @enum {string}
             */
            kind: "rig_telemetry" | "obd_snapshot";
        };
        /** IrregularWindow */
        IrregularWindow: {
            /** Rms Mg */
            rms_mg: number;
            /**
             * Status
             * @default unverified
             * @constant
             */
            status: "unverified";
            /** T Ms */
            t_ms: number;
        };
        /** LockedZoneSummary */
        LockedZoneSummary: {
            /** Finding Count */
            finding_count: number;
            /**
             * Locked
             * @default true
             * @constant
             */
            locked: true;
            zone: components["schemas"]["Zone"];
        };
        /** LoginRequest */
        LoginRequest: {
            /** Email */
            email: string;
            /** Password */
            password: string;
        };
        /** NarratedStatement */
        NarratedStatement: {
            /** Finding Ids */
            finding_ids: string[];
            /** Text */
            text: string;
        };
        /** ObdSnapshotData */
        ObdSnapshotData: {
            /**
             * Distance Since Codes Cleared Km
             * @default null
             */
            distance_since_codes_cleared_km: number | null;
            /**
             * Dtcs
             * @default []
             */
            dtcs: components["schemas"]["DtcEntry"][];
            /** Mil On */
            mil_on: boolean;
            /**
             * Pids
             * @default {}
             */
            pids: {
                [key: string]: number;
            };
            /**
             * Readiness
             * @default {}
             */
            readiness: {
                [key: string]: "complete" | "incomplete" | "unsupported";
            };
            /**
             * Vin
             * @default null
             */
            vin: string | null;
            /**
             * Warmups Since Codes Cleared
             * @default null
             */
            warmups_since_codes_cleared: number | null;
        };
        /** ObdSnapshotSource */
        ObdSnapshotSource: {
            /**
             * Captured At
             * Format: date-time
             */
            captured_at: string;
            data: components["schemas"]["ObdSnapshotData"];
            /** Device */
            device: string;
            /**
             * Kind
             * @constant
             */
            kind: "obd_snapshot";
            /**
             * Provenance
             * @enum {string}
             */
            provenance: "synthetic" | "cached" | "live";
        };
        /** ObdSummary */
        ObdSummary: {
            /**
             * Codes Recently Cleared
             * @default null
             */
            codes_recently_cleared: boolean | null;
            /**
             * Cold Start
             * @default null
             */
            cold_start: boolean | null;
            /** Config Hash */
            config_hash: string;
            /**
             * Coolant C
             * @default null
             */
            coolant_c: number | null;
            /**
             * Distance Since Codes Cleared Km
             * @default null
             */
            distance_since_codes_cleared_km: number | null;
            /**
             * Dtcs
             * @default []
             */
            dtcs: components["schemas"]["DtcEntry"][];
            /**
             * Flag Count
             * @default 0
             */
            flag_count: number;
            /**
             * Flags
             * @default []
             */
            flags: string[];
            /**
             * Incomplete Monitors
             * @default []
             */
            incomplete_monitors: string[];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "obd_snapshot";
            /** Mil On */
            mil_on: boolean;
            /**
             * Provenance
             * @enum {string}
             */
            provenance: "synthetic" | "cached" | "live";
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            /**
             * State
             * @default assessed
             * @enum {string}
             */
            state: "assessed" | "not_assessed";
            /**
             * Uncalibrated Note
             * @default Values are uncalibrated estimates, not measurements.
             * @constant
             */
            uncalibrated_note: "Values are uncalibrated estimates, not measurements.";
            /**
             * Vin
             * @default null
             */
            vin: string | null;
            /**
             * Warmups Since Codes Cleared
             * @default null
             */
            warmups_since_codes_cleared: number | null;
        };
        /** PaidFinding */
        PaidFinding: {
            bbox: components["schemas"]["BoundingBox"];
            component_id: components["schemas"]["ComponentId"] | null;
            concept: components["schemas"]["Concept"];
            /** Confidence */
            confidence: number | null;
            /**
             * Confidence Label
             * @default Uncalibrated model confidence, not an accuracy probability
             * @constant
             */
            confidence_label: "Uncalibrated model confidence, not an accuracy probability";
            /** Evidence Available */
            evidence_available: boolean;
            /** Evidence Note */
            evidence_note: string;
            /** Flags */
            flags: string[];
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Mapping Basis
             * @default null
             */
            mapping_basis: "user_declared" | null;
            /** Severity */
            severity: number;
            /**
             * Source Timestamp Ms
             * @default null
             */
            source_timestamp_ms: number | null;
            subzone: components["schemas"]["Subzone"] | null;
            zone: components["schemas"]["Zone"];
        };
        /** PaidReport */
        PaidReport: {
            /**
             * Additional Checks
             * @default []
             */
            additional_checks: components["schemas"]["PaidSourceCheck"][];
            /**
             * Candidates
             * @default []
             */
            candidates: components["schemas"]["CandidateFinding"][];
            /** Claims */
            claims: components["schemas"]["PublicClaim"][];
            /** Coverage */
            coverage: components["schemas"]["PublicCoverage"][];
            /** Findings */
            findings: components["schemas"]["PaidFinding"][];
            /** Narration */
            narration: components["schemas"]["NarratedStatement"][];
            /**
             * Rendered At
             * @default null
             */
            rendered_at: string | null;
            repairs: components["schemas"]["RepairSummary"];
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /**
             * Scope
             * @default paid
             * @constant
             */
            scope: "paid";
            scores: components["schemas"]["ScoreResult"];
            /** Slug */
            slug: string;
            state: components["schemas"]["ReportState"];
            /**
             * Vehicle Label
             * @default null
             */
            vehicle_label: string | null;
        };
        /** PaidSourceCheck */
        PaidSourceCheck: {
            /** Checked */
            checked: boolean;
            /**
             * Kind
             * @enum {string}
             */
            kind: "rig_telemetry" | "obd_snapshot";
            /** @default null */
            source: components["schemas"]["SourceRecord"] | null;
        };
        /**
         * Part1Verdict
         * @description Owner-progress free verdict. No report identity: before finalize no report exists,
         *     and afterwards `ProgressResponse.report` carries the slug and state.
         */
        Part1Verdict: {
            /** Coverage */
            coverage: components["schemas"]["PublicCoverage"][];
            /** Findings */
            findings: components["schemas"]["FreeFinding"][];
            /** Locked Zones */
            locked_zones: components["schemas"]["LockedZoneSummary"][];
            /** Part1 Score */
            part1_score: number | null;
            /** Part1 Summary */
            part1_summary: string;
        };
        /** PartAcceptedResponse */
        PartAcceptedResponse: {
            /**
             * Part Id
             * Format: uuid
             */
            part_id: string;
            state: components["schemas"]["PartState"];
        };
        /**
         * PartState
         * @enum {string}
         */
        PartState: "pending" | "uploading" | "uploaded" | "processing" | "processed" | "failed" | "skipped";
        /** PartSummary */
        PartSummary: {
            /**
             * Active Upload Id
             * @default null
             */
            active_upload_id: string | null;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Part No */
            part_no: number;
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            state: components["schemas"]["PartState"];
            zone: components["schemas"]["Zone"];
        };
        /** PasswordResetConfirmRequest */
        PasswordResetConfirmRequest: {
            /** New Password */
            new_password: string;
            /** Token */
            token: string;
        };
        /** PasswordResetRequest */
        PasswordResetRequest: {
            /** Email */
            email: string;
        };
        /**
         * PrivacyState
         * @enum {string}
         */
        PrivacyState: "pending" | "approved" | "withheld";
        /** Problem */
        Problem: {
            /**
             * Code
             * @enum {string}
             */
            code: "TOKEN_INVALID" | "NOT_FOUND" | "STATE_CONFLICT" | "UPLOAD_TOO_LARGE" | "UPLOAD_INCOMPLETE" | "RATE_LIMITED" | "VALIDATION" | "REPORT_NOT_READY" | "INTERNAL" | "EMAIL_TAKEN" | "INVALID_CREDENTIALS";
            /** Detail */
            detail: string;
            /**
             * Request Id
             * Format: uuid
             */
            request_id: string;
            /** Status */
            status: number;
            /** Title */
            title: string;
            /** Type */
            type: string;
        };
        /** ProgressResponse */
        ProgressResponse: {
            free_verdict: components["schemas"]["Part1Verdict"] | null;
            /** Parts */
            parts: components["schemas"]["PartSummary"][];
            report: components["schemas"]["ReportProgress"] | null;
            state: components["schemas"]["SessionState"];
        };
        /** PublicClaim */
        PublicClaim: {
            /** Claim Index */
            claim_index: number;
            /** Finding Ids */
            finding_ids: string[];
            /** Rationale */
            rationale: string;
            /** Text */
            text: string;
            verdict: components["schemas"]["ClaimVerdict"];
        };
        /** PublicCoverage */
        PublicCoverage: {
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            state: components["schemas"]["CoverageState"];
            zone: components["schemas"]["Zone"];
        };
        /**
         * PublicFinding
         * @description Evidence route identifies a validated finding, never a storage key/URL.
         */
        PublicFinding: {
            bbox: components["schemas"]["BoundingBox"];
            component_id: components["schemas"]["ComponentId"] | null;
            concept: components["schemas"]["Concept"];
            /** Confidence */
            confidence: number | null;
            /**
             * Confidence Label
             * @default Uncalibrated model confidence, not an accuracy probability
             * @constant
             */
            confidence_label: "Uncalibrated model confidence, not an accuracy probability";
            /** Evidence Available */
            evidence_available: boolean;
            /** Evidence Note */
            evidence_note: string;
            /** Flags */
            flags: string[];
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Mapping Basis
             * @default null
             */
            mapping_basis: "user_declared" | null;
            /** Severity */
            severity: number;
            /**
             * Source Timestamp Ms
             * @default null
             */
            source_timestamp_ms: number | null;
            subzone: components["schemas"]["Subzone"] | null;
            zone: components["schemas"]["Zone"];
        };
        /** RegisterRequest */
        RegisterRequest: {
            /** Email */
            email: string;
            /** Password */
            password: string;
        };
        /** RepairEstimate */
        RepairEstimate: {
            component_id: components["schemas"]["ComponentId"] | null;
            /** Finding Ids */
            finding_ids: string[];
            /** High Cents */
            high_cents: number | null;
            /** Low Cents */
            low_cents: number | null;
            operation: components["schemas"]["RepairOperation"] | null;
            /** Reason */
            reason: string | null;
            /** Severity */
            severity: number;
        };
        /**
         * RepairOperation
         * @enum {string}
         */
        RepairOperation: "replace_tire" | "replace_windshield" | "repair_rocker" | "paint_panel";
        /** RepairSummary */
        RepairSummary: {
            /** Items */
            items: components["schemas"]["RepairEstimate"][];
            /**
             * Label
             * @default Local planning estimate; not a current shop quote
             */
            label: string;
            /** Subtotal High Cents */
            subtotal_high_cents: number;
            /** Subtotal Low Cents */
            subtotal_low_cents: number;
            /** Table Version */
            table_version: string;
            /** Unestimated Count */
            unestimated_count: number;
        };
        /** ReportProgress */
        ReportProgress: {
            /** Slug */
            slug: string;
            state: components["schemas"]["ReportState"];
            /**
             * Unlocked
             * @default false
             */
            unlocked: boolean;
        };
        /**
         * ReportState
         * @enum {string}
         */
        ReportState: "building" | "ready" | "failed";
        /**
         * ReportStatus
         * @description A non-ready report exposes status only, never partial paid stage outputs.
         */
        ReportStatus: {
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /**
             * Scope
             * @enum {string}
             */
            scope: "free" | "paid";
            /** Slug */
            slug: string;
            /**
             * State
             * @enum {string}
             */
            state: "building" | "failed";
        };
        /** RigTelemetryData */
        RigTelemetryData: {
            accel: components["schemas"]["AccelData"];
            /**
             * Cylinders
             * @default 4
             */
            cylinders: number;
            /** @default null */
            sound: components["schemas"]["SoundData"] | null;
            /**
             * Temperature
             * @default []
             */
            temperature: components["schemas"]["TemperatureReading"][];
        };
        /** RigTelemetrySource */
        RigTelemetrySource: {
            /**
             * Captured At
             * Format: date-time
             */
            captured_at: string;
            data: components["schemas"]["RigTelemetryData"];
            /** Device */
            device: string;
            /**
             * Kind
             * @constant
             */
            kind: "rig_telemetry";
            /**
             * Provenance
             * @enum {string}
             */
            provenance: "synthetic" | "cached" | "live";
        };
        /** RigTelemetrySummary */
        RigTelemetrySummary: {
            /** Config Hash */
            config_hash: string;
            /** Cylinders */
            cylinders: number;
            /**
             * Dominant Hz
             * @default null
             */
            dominant_hz: number | null;
            /** Duration S */
            duration_s: number;
            /**
             * Flag Count
             * @default 0
             */
            flag_count: number;
            /**
             * Flags
             * @default []
             */
            flags: string[];
            /**
             * Irregular Windows
             * @default []
             */
            irregular_windows: components["schemas"]["IrregularWindow"][];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "rig_telemetry";
            /**
             * Provenance
             * @enum {string}
             */
            provenance: "synthetic" | "cached" | "live";
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            /**
             * Roughness
             * @default null
             */
            roughness: number | null;
            /**
             * Rpm Estimate
             * @default null
             */
            rpm_estimate: number | null;
            /** Sample Count */
            sample_count: number;
            /**
             * State
             * @enum {string}
             */
            state: "assessed" | "not_assessed";
            /** @default null */
            temperature: components["schemas"]["TemperatureSummary"] | null;
            /**
             * Uncalibrated Note
             * @default Values are uncalibrated estimates, not measurements.
             * @constant
             */
            uncalibrated_note: "Values are uncalibrated estimates, not measurements.";
        };
        /** ScoreResult */
        ScoreResult: {
            /** Config Hash */
            config_hash: string;
            /** Headline */
            headline: number | null;
            /**
             * Label
             * @default Provisional visual-condition index
             */
            label: string;
            /** Zones */
            zones: components["schemas"]["ZoneScore"][];
        };
        /** ScoringInput */
        ScoringInput: {
            /** Coverage */
            coverage: components["schemas"]["Coverage"][];
            /** Findings */
            findings: components["schemas"]["Finding"][];
            /** Run Ids */
            run_ids: string[];
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
        };
        /** SessionResponse */
        SessionResponse: {
            /**
             * Capture Mode
             * @default guided
             * @enum {string}
             */
            capture_mode: "guided" | "single_video";
            /**
             * Consent At
             * Format: date-time
             */
            consent_at: string;
            /** Listing Text */
            listing_text: string | null;
            /** Listing Version */
            listing_version: number;
            /** Parts */
            parts: components["schemas"]["PartSummary"][];
            /**
             * Purpose
             * @constant
             */
            purpose: "buyer";
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            state: components["schemas"]["SessionState"];
            /**
             * Vehicle Label
             * @default null
             */
            vehicle_label: string | null;
        };
        /**
         * SessionState
         * @enum {string}
         */
        SessionState: "capturing" | "finalized" | "deleting" | "deleted";
        /** ShareDisableResponse */
        ShareDisableResponse: {
            /**
             * Share Enabled
             * @default false
             * @constant
             */
            share_enabled: false;
        };
        /**
         * ShareEnableResponse
         * @description Owner-only share enable result. `url` is the existing public report
         *     path (`/r/{slug}`); `public_access_active` is
         *     `settings.public_sharing_enabled and share_enabled`, so callers can tell
         *     whether the link actually opens today while the global gate is off.
         */
        ShareEnableResponse: {
            /** Public Access Active */
            public_access_active: boolean;
            /**
             * Share Enabled
             * @default true
             * @constant
             */
            share_enabled: true;
            /** Url */
            url: string;
        };
        /** SkipPartResponse */
        SkipPartResponse: {
            /**
             * State
             * @default skipped
             * @constant
             */
            state: "skipped";
        };
        /** SoundData */
        SoundData: {
            /** Level */
            level: number[];
            /** Rate Hz */
            rate_hz: number;
        };
        /**
         * SourceRecord
         * @description One frozen source: summary plus the hash binding it to the raw row. Manifest and paid report.
         */
        SourceRecord: {
            /**
             * Captured At
             * Format: date-time
             */
            captured_at: string;
            /** Data Sha256 */
            data_sha256: string;
            /** Device */
            device: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "rig_telemetry" | "obd_snapshot";
            /**
             * Provenance
             * @enum {string}
             */
            provenance: "synthetic" | "cached" | "live";
            /** Summary */
            summary: components["schemas"]["RigTelemetrySummary"] | components["schemas"]["ObdSummary"];
        };
        /** SourceResponse */
        SourceResponse: {
            /** Data Sha256 */
            data_sha256: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "rig_telemetry" | "obd_snapshot";
            /**
             * Source Id
             * Format: uuid
             */
            source_id: string;
            /** Summary */
            summary: components["schemas"]["RigTelemetrySummary"] | components["schemas"]["ObdSummary"];
        };
        /**
         * Subzone
         * @enum {string}
         */
        Subzone: "rocker" | "door" | "fender" | "quarter_panel" | "hood" | "roof" | "trunk" | "bumper" | "windshield" | "tire" | "engine_bay" | "underbody" | "interior" | "dash";
        /** TemperatureReading */
        TemperatureReading: {
            /** Celsius */
            celsius: number;
            /** T Ms */
            t_ms: number;
        };
        /** TemperatureSummary */
        TemperatureSummary: {
            /** Engine Warm At Start */
            engine_warm_at_start: boolean;
            /** Rise C */
            rise_c: number | null;
            /** Start C */
            start_c: number;
        };
        /** UpdateListingRequest */
        UpdateListingRequest: {
            /** Listing Text */
            listing_text: string;
        };
        /** UpdateListingResponse */
        UpdateListingResponse: {
            /** Listing Version */
            listing_version: number;
        };
        /** UpdateVehicleLabelRequest */
        UpdateVehicleLabelRequest: {
            /** Label */
            label: string;
        };
        /** UploadedPiece */
        UploadedPiece: {
            /** Etag */
            etag: string;
            /** Part Number */
            part_number: number;
            /** Size Bytes */
            size_bytes: number;
        };
        /** UploadResponse */
        UploadResponse: {
            /** Pieces */
            pieces: components["schemas"]["UploadedPiece"][];
            state: components["schemas"]["UploadState"];
            /**
             * Upload Id
             * Format: uuid
             */
            upload_id: string;
        };
        /**
         * UploadState
         * @enum {string}
         */
        UploadState: "open" | "completing" | "accepted" | "aborted";
        /** UploadUrl */
        UploadUrl: {
            /**
             * Expires At
             * Format: date-time
             */
            expires_at: string;
            /** Headers */
            headers: {
                [key: string]: string;
            };
            /** Part Number */
            part_number: number;
            /**
             * Url
             * Format: uri
             */
            url: string;
        };
        /** UploadUrlsRequest */
        UploadUrlsRequest: {
            /** Part Numbers */
            part_numbers: number[];
        };
        /** UploadUrlsResponse */
        UploadUrlsResponse: {
            /** Items */
            items: components["schemas"]["UploadUrl"][];
        };
        /**
         * VerificationGroup
         * @description Construct from trusted input IDs plus untrusted parsed verdicts.
         */
        VerificationGroup: {
            /** Candidate Ids */
            candidate_ids: string[];
            /** Verdicts */
            verdicts: components["schemas"]["VerificationVerdict"][];
        };
        /**
         * VerificationStatus
         * @enum {string}
         */
        VerificationStatus: "confirmed" | "artifact" | "wrong_type" | "duplicate" | "unverifiable";
        /** VerificationVerdict */
        VerificationVerdict: {
            /** @default null */
            component_id: components["schemas"]["ComponentId"] | null;
            concept: components["schemas"]["Concept"];
            /**
             * Confidence
             * @default null
             */
            confidence: number | null;
            /**
             * Duplicate Of
             * @default null
             */
            duplicate_of: string | null;
            /** Evidence Note */
            evidence_note: string;
            /**
             * Finding Id
             * Format: uuid
             */
            finding_id: string;
            /**
             * Flags
             * @default []
             */
            flags: string[];
            /**
             * Severity
             * @default null
             */
            severity: number | null;
            /** @default null */
            subzone: components["schemas"]["Subzone"] | null;
            verdict: components["schemas"]["VerificationStatus"];
        };
        /** VideoAcceptedResponse */
        VideoAcceptedResponse: {
            /**
             * Processing State
             * @enum {string}
             */
            processing_state: "pending" | "queued" | "ready" | "failed";
            state: components["schemas"]["UploadState"];
            /**
             * Upload Id
             * Format: uuid
             */
            upload_id: string;
        };
        /** VideoRange */
        VideoRange: {
            /** End Ms */
            end_ms: number;
            /** Part No */
            part_no: number;
            /** Start Ms */
            start_ms: number;
        };
        /** VideoSourceResponse */
        VideoSourceResponse: {
            /** @default null */
            source: components["schemas"]["VideoUploadResponse"] | null;
        };
        /** VideoUploadResponse */
        VideoUploadResponse: {
            /**
             * Content Type
             * @enum {string}
             */
            content_type: "video/mp4" | "video/webm" | "video/quicktime";
            /**
             * Duration Ms
             * @default null
             */
            duration_ms: number | null;
            /** Expected Bytes */
            expected_bytes: number;
            /**
             * Pieces
             * @default []
             */
            pieces: components["schemas"]["UploadedPiece"][];
            /**
             * Processing State
             * @enum {string}
             */
            processing_state: "pending" | "queued" | "ready" | "failed";
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            /**
             * Segments
             * @default []
             */
            segments: components["schemas"]["VideoRange"][];
            state: components["schemas"]["UploadState"];
            /**
             * Upload Id
             * Format: uuid
             */
            upload_id: string;
        };
        /**
         * Zone
         * @enum {string}
         */
        Zone: "driver_side" | "passenger_side" | "front_rear_body" | "tires" | "dash_electrical" | "engine_bay" | "underbody" | "interior";
        /** ZoneScore */
        ZoneScore: {
            /**
             * Reason
             * @default null
             */
            reason: string | null;
            /** Score */
            score: number | null;
            state: components["schemas"]["CoverageState"];
            zone: components["schemas"]["Zone"];
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export type operations = Record<string, never>;
