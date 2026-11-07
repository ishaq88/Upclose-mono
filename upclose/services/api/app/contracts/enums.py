from enum import StrEnum


class Zone(StrEnum):
    DRIVER_SIDE = "driver_side"
    PASSENGER_SIDE = "passenger_side"
    FRONT_REAR_BODY = "front_rear_body"
    TIRES = "tires"
    DASH_ELECTRICAL = "dash_electrical"
    ENGINE_BAY = "engine_bay"
    UNDERBODY = "underbody"
    INTERIOR = "interior"


PART_ZONES = tuple(Zone)


class Concept(StrEnum):
    RUST = "rust"
    PAINT_BUBBLING = "paint_bubbling"
    DENT = "dent"
    SCRATCH = "scratch"
    GLASS_CRACK = "glass_crack"
    TIRE_WEAR = "tire_wear"
    FLUID_LEAK = "fluid_leak"


class ComponentId(StrEnum):
    DRIVER_ROCKER = "driver_rocker"
    PASSENGER_ROCKER = "passenger_rocker"
    WINDSHIELD = "windshield"
    TIRE_FL = "tire_fl"
    TIRE_FR = "tire_fr"
    TIRE_RL = "tire_rl"
    TIRE_RR = "tire_rr"
    DRIVER_FRONT_DOOR = "driver_front_door"
    DRIVER_REAR_DOOR = "driver_rear_door"
    PASSENGER_FRONT_DOOR = "passenger_front_door"
    PASSENGER_REAR_DOOR = "passenger_rear_door"
    HOOD = "hood"
    ROOF = "roof"
    TRUNK = "trunk"
    FRONT_BUMPER = "front_bumper"
    REAR_BUMPER = "rear_bumper"


class Subzone(StrEnum):
    ROCKER = "rocker"
    DOOR = "door"
    FENDER = "fender"
    QUARTER_PANEL = "quarter_panel"
    HOOD = "hood"
    ROOF = "roof"
    TRUNK = "trunk"
    BUMPER = "bumper"
    WINDSHIELD = "windshield"
    TIRE = "tire"
    ENGINE_BAY = "engine_bay"
    UNDERBODY = "underbody"
    INTERIOR = "interior"
    DASH = "dash"


class SessionState(StrEnum):
    CAPTURING = "capturing"
    FINALIZED = "finalized"
    DELETING = "deleting"
    DELETED = "deleted"


class PartState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class UploadState(StrEnum):
    OPEN = "open"
    COMPLETING = "completing"
    ACCEPTED = "accepted"
    ABORTED = "aborted"


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"
    ARTIFACT = "artifact"
    WRONG_TYPE = "wrong_type"
    DUPLICATE = "duplicate"
    UNVERIFIABLE = "unverifiable"


class CoverageState(StrEnum):
    ASSESSED = "assessed"
    PARTIAL = "partial"
    NOT_ASSESSED = "not_assessed"


class ConceptRunState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ClaimVerdict(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    NOT_VISIBLE = "not_visible"


class ReportState(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class PrivacyState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    WITHHELD = "withheld"


class RepairOperation(StrEnum):
    REPLACE_TIRE = "replace_tire"
    REPLACE_WINDSHIELD = "replace_windshield"
    REPAIR_ROCKER = "repair_rocker"
    PAINT_PANEL = "paint_panel"
