"""Stripe adapter: typed interface, synthetic fixture, and a refusing test-mode client.

Nothing outside this module imports the Stripe SDK. Verified against stripe==16.0.1:

* Webhook authenticity uses ``stripe.WebhookSignature.verify_header`` (HMAC-SHA256 over
  ``"{t}.{raw body}"``, 300 s default tolerance). Only afterwards is the body parsed.
* ``StripeClient(..., max_network_retries=0)`` disables hidden SDK retries (the default
  ``None`` is treated as 0 by ``HTTPClient._should_retry``; it is set explicitly anyway),
  so every retry is a visible, counted job attempt. The caller's purchase ID is the
  Stripe idempotency key, so an application-level retry cannot create a second session.
* The live client refuses anything except ``sk_test_``/``rk_test_`` keys and any response
  with ``livemode=true``. Fixture mode never opens a socket.

Payloads and keys are never logged here. Provider exceptions are translated to three
sanitized classes whose messages carry no provider text.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID

import stripe

MAX_WEBHOOK_BYTES = 512 * 1024
WEBHOOK_TOLERANCE_SECONDS = 300
_ALLOWED_KEY_PREFIXES = ("sk_test_", "rk_test_")


class ProviderError(Exception):
    """Sanitized provider failure. Message never contains provider text."""


class ProviderUnavailable(ProviderError):
    """Network, 5xx, rate limit or in-flight idempotent request: retry is meaningful."""


class ProviderNotFound(ProviderError):
    """The provider has no such object."""


class ProviderRejected(ProviderError):
    """Credentials, permissions, live-mode refusal or invalid request: retry cannot help."""


class InvalidWebhook(Exception):
    """Signature or envelope failed validation. Message is a stable non-secret code."""


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    purchase_id: UUID
    report_id: UUID
    amount_cents: int
    currency: str
    success_url: str
    cancel_url: str
    price_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedCheckout:
    checkout_id: str
    url: str
    synthetic: bool


@dataclass(frozen=True, slots=True)
class ProviderCheckout:
    """Current provider truth for one Checkout Session (and its payment's refunds)."""
    checkout_id: str
    status: str  # open | complete | expired
    payment_status: str  # unpaid | paid | no_payment_required
    mode: str
    livemode: bool
    amount_total: int | None
    currency: str | None
    payment_intent_id: str | None
    client_reference_id: str | None
    metadata: Mapping[str, str] = field(default_factory=dict)
    amount_refunded: int = 0


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    event_id: str
    event_type: str
    livemode: bool
    payload: dict[str, Any]  # minimized projection; no customer details


class StripeGateway(Protocol):
    synthetic: bool

    def create_checkout(self, request: CheckoutRequest) -> CreatedCheckout: ...

    def retrieve_checkout(self, checkout_id: str) -> ProviderCheckout: ...

    def checkout_id_for_payment_intent(self, payment_intent_id: str) -> str | None: ...


# ---------------------------------------------------------------- webhook intake

_OBJECT_FIELDS = ("id", "object", "payment_status", "status", "mode", "amount_total", "amount",
                  "amount_refunded", "currency", "livemode", "client_reference_id", "refunded")


def _ref_id(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return value["id"]
    return None


def _minimize(obj: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {key: obj[key] for key in _OBJECT_FIELDS
                            if isinstance(obj.get(key), (str, int, bool))}
    intent = _ref_id(obj.get("payment_intent"))
    if intent is not None:
        kept["payment_intent"] = intent
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        kept["metadata"] = {k: v for k, v in metadata.items()
                            if k in ("purchase_id", "report_id") and isinstance(v, str)}
    return kept


def verify_webhook(raw_body: bytes, signature_header: str | None, secret: str | None) -> WebhookEvent:
    """Verify over the exact raw bytes, then parse a minimal envelope.

    Raises ``InvalidWebhook`` for a missing/invalid signature, stale timestamp, oversize
    body or malformed envelope. Never returns an unverified body.
    """
    if not secret:
        raise InvalidWebhook("secret_not_configured")
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise InvalidWebhook("body_too_large")
    try:
        stripe.WebhookSignature.verify_header(raw_body, signature_header, secret, WEBHOOK_TOLERANCE_SECONDS)
        envelope = json.loads(raw_body)
    except (stripe.SignatureVerificationError, ValueError):
        raise InvalidWebhook("invalid_signature") from None
    if not isinstance(envelope, dict) or envelope.get("object") != "event":
        raise InvalidWebhook("invalid_envelope")
    event_id, event_type, livemode = envelope.get("id"), envelope.get("type"), envelope.get("livemode")
    data = envelope.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if (not isinstance(event_id, str) or not 0 < len(event_id) <= 255 or not isinstance(event_type, str)
            or not 0 < len(event_type) <= 128 or not isinstance(livemode, bool) or not isinstance(obj, dict)):
        raise InvalidWebhook("invalid_envelope")
    payload = {"id": event_id, "type": event_type, "livemode": livemode,
               "created": envelope["created"] if isinstance(envelope.get("created"), int) else None,
               "data": {"object": _minimize(obj)}}
    return WebhookEvent(event_id, event_type, livemode, payload)


def sign_payload(payload: str, secret: str, timestamp: int | None = None) -> str:
    """Test helper: Stripe-Signature header for a synthetic body and a test secret."""
    return stripe.WebhookSignature.generate_signature_header(payload, secret, timestamp)


# ---------------------------------------------------------------- fixture gateway

class FixtureStripe:
    """Synthetic in-process gateway. All ids are labeled ``synthetic``.

    State lives in this process only, so a separate worker process cannot see sessions
    created by the API process; fixture mode is for tests and single-process demos.
    The ``simulate_*`` methods stand in for the customer and Stripe; they are not part
    of ``StripeGateway`` and nothing in the application calls them.
    """

    synthetic = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_purchase: dict[UUID, CreatedCheckout] = {}
        self._sessions: dict[str, ProviderCheckout] = {}
        self._intent_owner: dict[str, str] = {}
        self.create_calls = 0
        self.retrieve_calls = 0
        self.last_idempotency_key: str | None = None
        self.retrieve_failures: list[ProviderError] = []  # popped one per retrieve call
        self.create_failures: list[ProviderError] = []

    def create_checkout(self, request: CheckoutRequest) -> CreatedCheckout:
        with self._lock:
            self.create_calls += 1
            self.last_idempotency_key = str(request.purchase_id)
            if self.create_failures:
                raise self.create_failures.pop(0)
            existing = self._by_purchase.get(request.purchase_id)
            if existing is not None:  # same idempotency key: same session
                return existing
            digest = hashlib.sha256(request.purchase_id.bytes).hexdigest()[:24]
            checkout_id = f"cs_test_synthetic_{digest}"
            created = CreatedCheckout(checkout_id, f"https://checkout.fixture.invalid/synthetic/{checkout_id}", True)
            self._by_purchase[request.purchase_id] = created
            self._sessions[checkout_id] = ProviderCheckout(
                checkout_id=checkout_id, status="open", payment_status="unpaid", mode="payment",
                livemode=False, amount_total=request.amount_cents, currency=request.currency,
                payment_intent_id=None, client_reference_id=str(request.purchase_id),
                metadata={"purchase_id": str(request.purchase_id), "report_id": str(request.report_id)})
            return created

    def retrieve_checkout(self, checkout_id: str) -> ProviderCheckout:
        with self._lock:
            self.retrieve_calls += 1
            if self.retrieve_failures:
                raise self.retrieve_failures.pop(0)
            try:
                return self._sessions[checkout_id]
            except KeyError:
                raise ProviderNotFound("checkout_not_found") from None

    def checkout_id_for_payment_intent(self, payment_intent_id: str) -> str | None:
        with self._lock:
            return self._intent_owner.get(payment_intent_id)

    # ---- synthetic customer/provider controls (tests only)

    def simulate_completion(self, checkout_id: str, *, paid: bool = True, **overrides: Any) -> ProviderCheckout:
        """Customer finished Checkout. ``paid=False`` models a delayed (async) payment method."""
        with self._lock:
            current = self._sessions[checkout_id]
            intent = overrides.pop("payment_intent_id", None) or current.payment_intent_id or \
                "pi_test_synthetic_" + hashlib.sha256(checkout_id.encode()).hexdigest()[:24]
            updated = replace(current, status="complete", payment_status="paid" if paid else "unpaid",
                              payment_intent_id=intent, **overrides)
            self._sessions[checkout_id] = updated
            self._intent_owner[intent] = checkout_id
            return updated

    def simulate_refund(self, checkout_id: str, amount_cents: int, *, payment_intent_id: str | None = None) -> ProviderCheckout:
        with self._lock:
            current = self._sessions[checkout_id]
            intent = payment_intent_id or current.payment_intent_id or \
                "pi_test_synthetic_" + hashlib.sha256(checkout_id.encode()).hexdigest()[:24]
            updated = replace(current, payment_intent_id=intent,
                              amount_refunded=current.amount_refunded + amount_cents)
            self._sessions[checkout_id] = updated
            self._intent_owner[intent] = checkout_id
            return updated

    def simulate_override(self, checkout_id: str, **fields: Any) -> ProviderCheckout:
        with self._lock:
            updated = replace(self._sessions[checkout_id], **fields)
            self._sessions[checkout_id] = updated
            return updated


# ---------------------------------------------------------------- test-mode gateway

def _translate(error: Exception) -> ProviderError:
    if isinstance(error, stripe.InvalidRequestError) and error.http_status == 404:
        return ProviderNotFound("provider_not_found")
    if isinstance(error, (stripe.AuthenticationError, stripe.PermissionError, stripe.InvalidRequestError)):
        return ProviderRejected("provider_rejected")
    return ProviderUnavailable("provider_unavailable")


class StripeTestModeGateway:
    """Real Stripe API, test keys only. Not constructed by default or by any test here."""

    synthetic = False

    def __init__(self, secret_key: str, *, timeout_seconds: float = 10.0) -> None:
        if not secret_key.startswith(_ALLOWED_KEY_PREFIXES):
            raise ValueError("Only Stripe test-mode keys are accepted")
        http_client: stripe.HTTPClient | None
        try:
            http_client = stripe.RequestsClient(timeout=(3.05, timeout_seconds))
        except ImportError:
            http_client = None
        self._client = stripe.StripeClient(secret_key, max_network_retries=0, http_client=http_client)

    def create_checkout(self, request: CheckoutRequest) -> CreatedCheckout:
        line_item: dict[str, Any] = ({"price": request.price_id, "quantity": 1} if request.price_id else {
            "quantity": 1, "price_data": {"currency": request.currency, "unit_amount": request.amount_cents,
                                          "product_data": {"name": "UpClose report unlock"}}})
        metadata = {"purchase_id": str(request.purchase_id), "report_id": str(request.report_id)}
        params: dict[str, Any] = {
            "mode": "payment", "payment_method_types": ["card"], "line_items": [line_item],
            "client_reference_id": str(request.purchase_id), "metadata": metadata,
            "payment_intent_data": {"metadata": metadata},
            "success_url": request.success_url, "cancel_url": request.cancel_url}
        try:
            session = self._client.v1.checkout.sessions.create(
                params, {"idempotency_key": str(request.purchase_id), "max_network_retries": 0})  # type: ignore[arg-type]
        except stripe.StripeError as error:
            raise _translate(error) from None
        if session.livemode is not False or not session.url:
            raise ProviderRejected("live_mode_or_missing_url")
        return CreatedCheckout(session.id, session.url, False)

    def retrieve_checkout(self, checkout_id: str) -> ProviderCheckout:
        try:
            session = self._client.v1.checkout.sessions.retrieve(
                checkout_id, {"expand": ["payment_intent.latest_charge"]},  # type: ignore[arg-type]
                {"max_network_retries": 0})
        except stripe.StripeError as error:
            raise _translate(error) from None
        intent = session.payment_intent
        refunded = 0
        intent_id: str | None = None
        if isinstance(intent, str):
            intent_id = intent
        elif intent is not None:
            intent_id = intent.id
            charge = intent.latest_charge
            if charge is not None and not isinstance(charge, str):
                refunded = int(charge.amount_refunded or 0)
        return ProviderCheckout(
            checkout_id=session.id, status=str(session.status), payment_status=str(session.payment_status),
            mode=str(session.mode), livemode=bool(session.livemode), amount_total=session.amount_total,
            currency=session.currency, payment_intent_id=intent_id,
            client_reference_id=session.client_reference_id,
            metadata={str(k): str(v) for k, v in dict(session.metadata or {}).items()},
            amount_refunded=refunded)

    def checkout_id_for_payment_intent(self, payment_intent_id: str) -> str | None:
        try:
            found = self._client.v1.checkout.sessions.list(
                {"payment_intent": payment_intent_id, "limit": 1}, {"max_network_retries": 0})  # type: ignore[arg-type]
        except stripe.StripeError as error:
            raise _translate(error) from None
        return found.data[0].id if found.data else None


def build_gateway(mode: str, secret_key: str | None) -> StripeGateway:
    """Fixture by default. Test mode needs an explicit mode and a ``sk_test_``/``rk_test_`` key."""
    if mode == "fixture":
        return FixtureStripe()
    if mode == "test":
        if not secret_key:
            raise ValueError("PAYMENTS_MODE=test requires STRIPE_SECRET_KEY (a test-mode key)")
        return StripeTestModeGateway(secret_key)
    raise ValueError("unsupported payments mode")
