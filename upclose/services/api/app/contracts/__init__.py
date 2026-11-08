"""Pydantic is the authority for wire and domain contract schemas."""
from .api import (
    CheckoutRequest, CheckoutResponse, CreateSessionRequest, CreateSessionResponse,
    CreateUploadRequest, CreateUploadResponse, DeleteMediaResponse, EmptyRequest,
    FinalizeResponse, PartAcceptedResponse, PartSummary, Problem, ProgressResponse,
    ReportProgress, SessionResponse, SkipPartResponse, UpdateListingRequest,
    UpdateListingResponse, UpdateVehicleLabelRequest, UploadResponse, UploadUrl, UploadUrlsRequest,
    UploadUrlsResponse, UploadedPiece,
)
from .accounts import (
    AccountSessionSummary, AccountSessionsResponse, AccountSummary, AuthResponse,
    ChangePasswordRequest, ClaimSessionRequest, ClaimSessionResponse, LoginRequest,
    PasswordResetConfirmRequest, PasswordResetRequest, RegisterRequest,
)
from .base import ContractModel, FixtureMetadata
from .video import (VideoRange, CreateVideoUploadRequest, CreateVideoUploadResponse,
                    VideoUploadResponse, VideoSourceResponse, VideoAcceptedResponse)
from .enums import (
    ClaimVerdict, ComponentId, Concept, ConceptRunState, CoverageState, FindingStatus,
    PartState, PrivacyState, RepairOperation, ReportState, SessionState, Subzone,
    UploadState, VerificationStatus, Zone,
)
from .evidence import (
    Artifact, BoundingBox, ClaimGroup, ClaimResult, ConceptRun, Coverage, EvidenceRef,
    Finding, NarratedStatement, VerificationGroup, VerificationVerdict,
)
from .reports import (
    FreeFinding, FreeReport, LockedZoneSummary, PaidFinding, PaidReport, Part1Verdict, PublicCoverage,
    PublicFinding, PublicClaim, ReportStatus, ShareDisableResponse, ShareEnableResponse,
)
from .sources import (
    FreeSourceCheck, ObdSnapshotSource, PaidSourceCheck, RigTelemetrySource, EngineAudioSource, SourceListItem, SourceListResponse, SourceRecord, SourceResponse,
)
from .scoring import RepairEstimate, RepairSummary, ScoreResult, ScoringInput, ZoneScore

# Only explicit imported contract types are exported; no second enum registry.
__all__ = sorted(
    name for name, value in globals().copy().items()
    if isinstance(value, type) and not name.startswith("_")
)
