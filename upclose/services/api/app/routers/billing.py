"""Checkout creation (owner only) and the signed Stripe webhook inbox.

Browser navigation after Checkout grants nothing: there is deliberately no route that
reads a return URL, query string or client claim as payment evidence.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.stripe import InvalidWebhook, MAX_WEBHOOK_BYTES, StripeGateway, verify_webhook
from app.contracts.base import ContractModel
from app.routers.sessions import owner_bearer
from app.services.billing import CheckoutService, record_event
from app.services.problems import ProblemError
from app.settings import Settings


class CheckoutRequestBody(ContractModel):
    product: Literal["report_unlock"]


class CheckoutResponse(ContractModel):
    checkout_url: str | None = None
    unlocked: bool = False


def create_billing_router(sessions: sessionmaker[DBSession], settings: Settings, gateway: StripeGateway) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    checkout = CheckoutService(sessions, settings, gateway)
    secret = settings.stripe_webhook_secret.get_secret_value() if settings.stripe_webhook_secret else None

    @router.post("/reports/{slug}/checkout", response_model=CheckoutResponse, status_code=201)
    def create_checkout(slug: str, body: CheckoutRequestBody,
                        token: Annotated[str, Depends(owner_bearer)],
                        idempotency_key: Annotated[str | None, Header()] = None) -> CheckoutResponse:
        url, unlocked = checkout.create(token, slug, idempotency_key)
        return CheckoutResponse(checkout_url=url, unlocked=unlocked)

    @router.post("/webhooks/stripe", status_code=200)
    async def stripe_webhook(request: Request, stripe_signature: Annotated[str | None, Header()] = None) -> Response:
        if secret is None:
            raise ProblemError(503, "INTERNAL", "Webhook unavailable", "Webhook verification is not configured.")
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
            raise ProblemError(400, "VALIDATION", "Invalid webhook", "The webhook could not be verified.")
        raw = await request.body()  # exact bytes: the signature covers them
        try:
            event = verify_webhook(raw, stripe_signature, secret)
        except InvalidWebhook:
            raise ProblemError(400, "VALIDATION", "Invalid webhook", "The webhook could not be verified.") from None
        # Database errors propagate to the app-level SQLAlchemyError handler (503), so
        # Stripe retries. Success is returned only after the inbox row and job commit.
        await run_in_threadpool(record_event, sessions, event)
        return Response(status_code=200)

    return router
