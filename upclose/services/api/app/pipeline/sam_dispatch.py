"""Shared per-request SAM reservation, dispatch and settlement for both inference groups."""
import logging
from uuid import UUID, uuid5, NAMESPACE_URL
from app.adapters.meta import MediaRef, ProviderFailure, SegmentResult, Segmenter
from app.jobs.queue import Lease
from app.services.budgets import (PRICE_VERSION as BUDGET_PRICE_VERSION, BudgetError, BudgetService,
                                  ReservationRequest, UsageUnits, documented_prices, price_usage)


def run_sam_request(lease: Lease, segmenter: Segmenter, media: MediaRef, request_id: UUID,
                    prompt_id: str, budget_service: BudgetService | None) -> SegmentResult:
    # One provider request with budget dispatch. Fixture segmenters skip budgeting entirely.
    effective_prompt = prompt_id
    if not getattr(segmenter, "requires_budget", False):
        return segmenter.segment(media, effective_prompt, request_id)
    if budget_service is None or lease.session_id is None:
        raise ProviderFailure("budget_not_reserved")
    units = UsageUnits(frames=len(media.frames)) if media.kind == "video" else UsageUnits(images=1)
    attempt_id = uuid5(NAMESPACE_URL, f"budget:{lease.job_id}:{lease.attempt}:{effective_prompt}:{media.media_key}")
    request = ReservationRequest(lease.session_id, lease.job_id, attempt_id, lease.token, lease.deadline,
                                 "sam-3.1", BUDGET_PRICE_VERSION, units)
    try:
        budget_service.reserve(request)
        if not budget_service.mark_dispatched(attempt_id, lease.token):
            raise ProviderFailure("budget_not_reserved")
    except BudgetError as error:
        code = {"standard_access_unverified": "standard_access_unverified",
                "price_unverified": "price_unverified", "model_unverified": "model_unverified",
                "budget_exhausted": "budget_not_reserved"}.get(error.code, "provider_timeout")
        raise ProviderFailure(code) from None
    try:
        result = segmenter.segment(media, effective_prompt, request_id)
    except BaseException as error:
        failure_code = error.code if isinstance(error, ProviderFailure) else None
        http_status = error.http_status if isinstance(error, ProviderFailure) else None
        if isinstance(error, ProviderFailure):
            logging.warning('SAM request failed code=%s http_status=%s http_status_class=%s', failure_code, http_status, error.http_status_class)
        try:
            try:
                budget_service.mark_unknown(attempt_id, failure_code=failure_code, http_status=http_status)
            except TypeError:
                budget_service.mark_unknown(attempt_id)
        except BudgetError:
            pass
        raise
    usage = result.usage
    actual = None
    if media.kind == "video" and usage.video_frames_processed is not None:
        actual = UsageUnits(frames=usage.video_frames_processed)
    elif media.kind == "image" and usage.images_processed is not None:
        actual = UsageUnits(images=usage.images_processed)
    if actual is None:
        budget_service.mark_unknown(attempt_id)  # unknown billing stays reserved, never free
        return result
    try:
        budget_service.settle(attempt_id, actual_units=actual,
                                   actual_micro_usd=price_usage("sam-3.1", actual, documented_prices()),
                                   outcome="succeeded", evidence_code="meta-usage-reported")
    except BudgetError:
        logging.warning("Budget settlement deferred; ledger keeps unknown charge.")
    return result
