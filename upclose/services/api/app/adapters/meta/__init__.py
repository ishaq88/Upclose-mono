"""Meta boundaries: synthetic fixture segmentation and disabled live transport."""
from .adapters import (
    PRICE_VERSION, FixtureResponse, FixtureSegmenter, LiveConfiguration,
    LiveSegmenter, sam_request_micro_usd,
)
from .parser import ProviderFailure, normalize_output
from .types import (
    ClaimChecker, FrameRef, MaskRaster, MediaRef, Narrator, ProviderUsage,
    SegmentObservation, SegmentResult, Segmenter, Verifier,
)
