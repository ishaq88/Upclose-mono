from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.contracts.base import ContractModel
from .parser import ProviderFailure, normalize_output
from .types import MediaRef, ProviderUsage, SegmentResult


PRICE_VERSION = "meta-docs-2026-10-28"


def sam_request_micro_usd(*, images: int = 0, frames: int = 0) -> int:
    """Documented SAM request minimum; reservation transactions belong to T06b."""
    if type(images) is not int or type(frames) is not int or images < 0 or frames < 0:
        raise ValueError("usage must be nonnegative integer units")
    if bool(images) == bool(frames):
        raise ValueError("one request must contain exactly one media kind")
    return images * 2500 if images else max(frames, 50) * 200


class FixtureResponse(ContractModel):
    provenance: Literal["synthetic"]
    description: Annotated[str, Field(min_length=1, max_length=240)]
    status: Literal["completed", "incomplete", "timeout", "failed"]
    output_text: str
    usage: ProviderUsage


class FixtureSegmenter:
    def __init__(self, responses: dict[str, FixtureResponse]) -> None:
        self._responses = dict(responses)

    def segment(self, media_ref: MediaRef, prompt_id: str, request_id: UUID) -> SegmentResult:
        response = self._responses.get(prompt_id)
        if response is None:
            raise ProviderFailure("fixture_missing")
        if response.status in ("timeout", "failed"):
            raise ProviderFailure("provider_timeout" if response.status == "timeout" else "provider_failed")
        return normalize_output(response.output_text, media_ref, prompt_id, request_id,
                                usage=response.usage, completed=response.status == "completed")


class LiveConfiguration(ContractModel):
    model: str
    price_version: str | None = None
    standard_access_receipt: str | None = None
    capability_receipt: str | None = None
    budget_reservation_id: UUID | None = None


class LiveSegmenter:
    """No transport in T04a. Receipts/working reservation must precede T04b calls."""
    def __init__(self, configuration: LiveConfiguration) -> None:
        self.configuration = configuration

    def segment(self, media_ref: MediaRef, prompt_id: str, request_id: UUID) -> SegmentResult:
        config = self.configuration
        if config.model != "sam-3.1":
            raise ProviderFailure("model_unverified")
        if config.price_version != PRICE_VERSION:
            raise ProviderFailure("price_unverified")
        if not config.standard_access_receipt or not config.capability_receipt:
            raise ProviderFailure("standard_access_unverified")
        if config.budget_reservation_id is None:
            raise ProviderFailure("budget_not_reserved")
        # Strings/IDs alone do not establish verified access or a durable budget.
        raise ProviderFailure("live_transport_disabled")
