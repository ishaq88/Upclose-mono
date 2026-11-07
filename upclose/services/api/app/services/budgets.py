"""Atomic PostgreSQL spend reservations. All amounts use fake/local units in tests.

No method calls a provider. The caller must commit `mark_dispatched` before sending
once, never automatically retry transport, and settle verified usage afterward.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
import re
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.models import BudgetBucket, Job, ModelCall, Session


def budget_config_from_settings(settings) -> BudgetConfig:
    """Build the reservation guard from trusted server settings (never client JSON).

    Caps come from MODEL_SESSION_BUDGET_CENTS / MODEL_DAILY_BUDGET_CENTS so they change
    without code edits. Standard is verified only when a receipt is configured; the demo
    track needs the explicit non-Standard acknowledgement from the live-demo exception.
    """
    return BudgetConfig(model_mode=settings.model_mode,
                        session_limit_cents=settings.model_session_budget_cents,
                        daily_limit_cents=settings.model_daily_budget_cents,
                        price_version=settings.model_price_version,
                        verified_standard_access=bool(settings.model_standard_access_receipt),
                        standard_access_receipt=settings.model_standard_access_receipt,
                        allow_non_standard_demo=settings.model_allow_non_standard_demo)


PRICE_VERSION = "meta-docs-2026-10-28"
MICRO_USD_PER_CENT = 10000


class BudgetError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UsageUnits:
    images: int = 0
    frames: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in asdict(self).values()):
            raise BudgetError("invalid_usage")
        if self.cached_input_tokens > self.input_tokens:
            raise BudgetError("invalid_usage")


@dataclass(frozen=True)
class PriceRates:
    """Decimal micro-USD per unit, copied from dated documentation, not an API."""
    image: Decimal | None = None
    frame: Decimal | None = None
    input_token: Decimal | None = None
    cached_input_token: Decimal | None = None
    output_token: Decimal | None = None
    minimum_frames: int = 0

    def __post_init__(self) -> None:
        if type(self.minimum_frames) is not int or self.minimum_frames < 0:
            raise BudgetError("price_unverified")
        for rate in (self.image, self.frame, self.input_token, self.cached_input_token, self.output_token):
            if rate is not None and (not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0):
                raise BudgetError("price_unverified")


def documented_prices() -> dict[str, PriceRates]:
    spark = PriceRates(input_token=Decimal("1.25"), cached_input_token=Decimal("0.15"), output_token=Decimal("4.25"))
    return {"sam-3.1": PriceRates(image=Decimal(2500), frame=Decimal(200), minimum_frames=50),
            "muse-spark-1.3": spark, "muse-spark-1.2": spark, "muse-spark-1.1": spark}


@dataclass(frozen=True)
class BudgetConfig:
    model_mode: Literal["fixture", "live", "replay"] = "fixture"
    session_limit_cents: int = 200
    daily_limit_cents: int = 1000
    price_version: str = PRICE_VERSION
    prices: dict[str, PriceRates] = field(default_factory=documented_prices)
    # Only trusted operator configuration/evidence may set these, never client JSON.
    verified_standard_access: bool = False
    standard_access_receipt: str | None = None
    # Demo-only escape hatch for documented non-Standard free credits; see live-demo-exception.md.
    allow_non_standard_demo: bool = False

    def __post_init__(self) -> None:
        if self.model_mode not in {"fixture", "live", "replay"}:
            raise BudgetError("invalid_mode")
        if any(type(value) is not int or value < 0 for value in (self.session_limit_cents, self.daily_limit_cents)):
            raise BudgetError("invalid_limit")


@dataclass(frozen=True)
class ReservationRequest:
    session_id: UUID
    job_id: UUID
    attempt_id: UUID
    lease_token: UUID
    deadline_at: datetime
    model: str
    price_version: str
    units: UsageUnits

    def __post_init__(self) -> None:
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise BudgetError("deadline_requires_timezone")


@dataclass(frozen=True)
class ReservationResult:
    attempt_id: UUID
    call_id: UUID | None
    state: str
    reserved_micro_usd: int
    reserved_cents: int
    may_send: bool


def ceiling_cents(micro_usd: int) -> int:
    if type(micro_usd) is not int or micro_usd < 0:
        raise BudgetError("invalid_cost")
    return (micro_usd + MICRO_USD_PER_CENT - 1) // MICRO_USD_PER_CENT


def price_usage(model: str, units: UsageUnits, prices: dict[str, PriceRates]) -> int:
    rate = prices.get(model) if model in documented_prices() else None
    if rate is None:
        raise BudgetError("model_unverified")
    if model == "sam-3.1":
        if units.input_tokens or units.cached_input_tokens or units.output_tokens or bool(units.images) == bool(units.frames):
            raise BudgetError("invalid_usage")
        amounts = [(units.images, rate.image), (max(units.frames, 50, rate.minimum_frames) if units.frames else 0, rate.frame)]
    else:
        if units.images or units.frames:
            raise BudgetError("invalid_usage")
        amounts = [(units.input_tokens - units.cached_input_tokens, rate.input_token),
                   (units.cached_input_tokens, rate.cached_input_token), (units.output_tokens, rate.output_token)]
    # Missing rates fail even when the particular request's unit count is zero.
    required = (rate.image, rate.frame) if model == "sam-3.1" else (rate.input_token, rate.cached_input_token, rate.output_token)
    if any(value is None or not value.is_finite() or value < 0 for value in required):
        raise BudgetError("price_unverified")
    total = sum((Decimal(amount) * rate_ for amount, rate_ in amounts), Decimal(0))
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def _advisory(db: DBSession, attempt_id: UUID) -> None:
    key = int.from_bytes(sha256(attempt_id.bytes).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _db_time(db: DBSession) -> datetime:
    return db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)


def _owned_job(db: DBSession, session_id: UUID, job_id: UUID, lease_token: UUID, deadline: datetime) -> Job:
    # Match queue/deletion lock hierarchy: session first, then job.
    owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
    job = db.scalar(select(Job).where(Job.id == job_id, Job.session_id == session_id).with_for_update())
    now = _db_time(db)  # after lock waits, never transaction-start time
    if owner is None or owner.state in {"deleting", "deleted"} or job is None:
        raise BudgetError("job_not_eligible")
    if job.state != "running" or job.lease_token != lease_token or job.lease_until is None or job.lease_until <= now:
        raise BudgetError("lease_lost")
    if not 1 <= job.attempts <= 3:
        raise BudgetError("attempt_limit")
    if deadline <= now:
        raise BudgetError("deadline_expired")
    if job.attempt_started_at is None or job.attempt_started_at + timedelta(seconds=600) <= now:
        raise BudgetError("deadline_expired")
    return job


_SAFE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def sanitized_failure_record(failure_code: str | None, http_status: int | None) -> dict | None:
    """Sanitized provider-failure record for `model_calls.requested_units["failure"]`.

    Only an enumerated safe code and a numeric HTTP status/class survive; payloads,
    keys, headers and media references are never accepted here. Returns None when
    there is no failure to record (e.g. unknown usage on a successful response).
    """
    if failure_code is None:
        return None
    code = failure_code if isinstance(failure_code, str) and _SAFE_FAILURE_CODE.match(failure_code) else "provider_failed"
    status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
    return {"code": code, "http_status": status,
            "http_status_class": None if status is None else f"{status // 100}xx"}


def _metadata(call: ModelCall) -> dict:
    metadata = call.requested_units.get("budget")
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        raise BudgetError("reservation_metadata_invalid")
    return metadata


class BudgetService:
    def __init__(self, sessions: sessionmaker[DBSession], config: BudgetConfig | None = None) -> None:
        self.sessions = sessions
        self.config = config or BudgetConfig()

    def _configured_cost(self, request: ReservationRequest) -> int:
        config = self.config
        if config.price_version != PRICE_VERSION or request.price_version != config.price_version:
            raise BudgetError("price_unverified")
        standard_ok = config.verified_standard_access and config.standard_access_receipt
        if not standard_ok and not config.allow_non_standard_demo:
            raise BudgetError("standard_access_unverified")
        return price_usage(request.model, request.units, config.prices)

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        if self.config.model_mode != "live":
            return ReservationResult(request.attempt_id, None, self.config.model_mode, 0, 0, False)
        cost = self._configured_cost(request)
        reserved_cents = ceiling_cents(cost)
        fingerprint_data = {**asdict(request), "session_id": str(request.session_id), "job_id": str(request.job_id),
                            "attempt_id": str(request.attempt_id), "lease_token": str(request.lease_token),
                            "deadline_at": request.deadline_at.astimezone(timezone.utc).isoformat()}
        fingerprint = sha256(json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.sessions.begin() as db:
            _advisory(db, request.attempt_id)
            _owned_job(db, request.session_id, request.job_id, request.lease_token, request.deadline_at)
            existing = db.scalar(select(ModelCall).where(ModelCall.attempt_id == request.attempt_id))
            if existing is not None:
                metadata = _metadata(existing)
                if metadata["fingerprint"] != fingerprint or existing.reserved_cost != cost:
                    raise BudgetError("attempt_conflict")
                return self._result(existing, False)
            day = _db_time(db).date().isoformat()
            limits = {f"daily:{day}": self.config.daily_limit_cents,
                      f"session:{request.session_id}": self.config.session_limit_cents}
            for scope in sorted(limits):
                db.execute(insert(BudgetBucket).values(scope_key=scope, limit_cents=limits[scope]).on_conflict_do_nothing())
            buckets = list(db.scalars(select(BudgetBucket).where(BudgetBucket.scope_key.in_(limits)).order_by(BudgetBucket.scope_key).with_for_update()))
            for bucket in buckets:
                bucket.limit_cents = min(bucket.limit_cents, limits[bucket.scope_key])
            if any(bucket.spent_cents + bucket.reserved_cents + reserved_cents > bucket.limit_cents for bucket in buckets):
                raise BudgetError("budget_exhausted")
            # Recheck the DB clock after any bucket lock wait before granting a permit.
            _owned_job(db, request.session_id, request.job_id, request.lease_token, request.deadline_at)
            for bucket in buckets:
                bucket.reserved_cents += reserved_cents
            metadata = {"version": 1, "fingerprint": fingerprint, "scopes": sorted(limits),
                        "reserved_cents": reserved_cents, "lease_token": str(request.lease_token),
                        "deadline_at": request.deadline_at.astimezone(timezone.utc).isoformat()}
            call = ModelCall(session_id=request.session_id, job_id=request.job_id, attempt_id=request.attempt_id,
                             model=request.model, price_version=request.price_version,
                             requested_units={"units": asdict(request.units), "budget": metadata}, reserved_cost=cost, state="reserved")
            db.add(call)
            db.flush()
            return self._result(call, False)

    @staticmethod
    def _result(call: ModelCall, may_send: bool) -> ReservationResult:
        return ReservationResult(call.attempt_id, call.id, call.state, call.reserved_cost,
                                 _metadata(call)["reserved_cents"], may_send)

    def mark_dispatched(self, attempt_id: UUID, lease_token: UUID) -> bool:
        """Commit unknown-charge state before external I/O; one dispatch grant only."""
        if self.config.model_mode != "live":
            return False
        with self.sessions.begin() as db:
            _advisory(db, attempt_id)
            call = db.scalar(select(ModelCall).where(ModelCall.attempt_id == attempt_id))
            if call is None:
                raise BudgetError("reservation_not_found")
            metadata = _metadata(call)
            request = ReservationRequest(call.session_id, call.job_id, call.attempt_id, lease_token,
                                         datetime.fromisoformat(metadata["deadline_at"]), call.model, call.price_version,
                                         UsageUnits(**call.requested_units["units"]))
            if self._configured_cost(request) != call.reserved_cost:
                raise BudgetError("price_unverified")
            _owned_job(db, call.session_id, call.job_id, lease_token, datetime.fromisoformat(metadata["deadline_at"]))
            if metadata["lease_token"] != str(lease_token):
                raise BudgetError("lease_lost")
            if call.state != "reserved":
                return False
            buckets = list(db.scalars(select(BudgetBucket).where(BudgetBucket.scope_key.in_(metadata["scopes"]))
                                      .order_by(BudgetBucket.scope_key).with_for_update()))
            if len(buckets) != 2:
                raise BudgetError("reservation_metadata_invalid")
            for bucket in buckets:
                configured = self.config.daily_limit_cents if bucket.scope_key.startswith("daily:") else self.config.session_limit_cents
                if bucket.spent_cents + bucket.reserved_cents > min(bucket.limit_cents, configured):
                    raise BudgetError("budget_exhausted")
            _owned_job(db, call.session_id, call.job_id, lease_token, datetime.fromisoformat(metadata["deadline_at"]))
            if f"daily:{_db_time(db).date().isoformat()}" not in metadata["scopes"]:
                raise BudgetError("reservation_day_expired")
            call.state = "unknown"
            return True

    def mark_unknown(self, attempt_id: UUID, *, failure_code: str | None = None,
                     http_status: int | None = None) -> None:
        """Timeout/crash ambiguity never releases funds.

        Ledger state stays ``unknown`` (never free, never settled). When the
        dispatch raised a `ProviderFailure`, its sanitized code/status are
        persisted under ``requested_units["failure"]`` so a later reader can
        see *why* the call is unknown without any payload/key/media text.
        Reassigning the JSONB dict is required for SQLAlchemy change tracking.
        """
        record = sanitized_failure_record(failure_code, http_status)
        with self.sessions.begin() as db:
            _advisory(db, attempt_id)
            call = db.scalar(select(ModelCall).where(ModelCall.attempt_id == attempt_id))
            if call is None:
                raise BudgetError("reservation_not_found")
            if record is not None:
                call.requested_units = {**(call.requested_units or {}), "failure": record}
            if call.state == "reserved":
                call.state = "unknown"

    def settle(self, attempt_id: UUID, *, actual_units: UsageUnits | None,
               actual_micro_usd: int, outcome: Literal["succeeded", "failed", "reconciled"],
               evidence_code: str, provider_request_id: str | None = None,
               latency_ms: int | None = None) -> ReservationResult:
        """Trusted billing evidence only. Remains available after deletion/lease loss.

        Explicit cost is required: failed/time-out requests are never presumed free.
        Callers normalize verified provider billing state before invoking settlement.
        """
        actual_cents = ceiling_cents(actual_micro_usd)
        if outcome not in {"succeeded", "failed", "reconciled"} or not evidence_code or len(evidence_code) > 128:
            raise BudgetError("invalid_settlement")
        if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
            raise BudgetError("invalid_settlement")
        if provider_request_id is not None and (not provider_request_id or len(provider_request_id) > 255):
            raise BudgetError("invalid_settlement")
        evidence = {"units": asdict(actual_units) if actual_units is not None else None,
                    "micro_usd": actual_micro_usd, "outcome": outcome, "evidence_code": evidence_code,
                    "provider_request_id": provider_request_id, "latency_ms": latency_ms}
        with self.sessions.begin() as db:
            _advisory(db, attempt_id)
            call = db.scalar(select(ModelCall).where(ModelCall.attempt_id == attempt_id))
            if call is None:
                raise BudgetError("reservation_not_found")
            if call.actual_cost is not None:
                if call.actual_units != evidence:
                    raise BudgetError("settlement_conflict")
                return self._result(call, False)
            metadata = _metadata(call)
            scopes = metadata["scopes"]
            buckets = list(db.scalars(select(BudgetBucket).where(BudgetBucket.scope_key.in_(scopes)).order_by(BudgetBucket.scope_key).with_for_update()))
            if len(buckets) != 2 or any(bucket.reserved_cents < metadata["reserved_cents"] for bucket in buckets):
                raise BudgetError("reservation_metadata_invalid")
            for bucket in buckets:
                bucket.reserved_cents -= metadata["reserved_cents"]
                bucket.spent_cents += actual_cents
            call.actual_units = evidence
            call.actual_cost = actual_micro_usd
            call.provider_request_id = provider_request_id
            call.latency_ms = latency_ms
            call.state = outcome
            return self._result(call, False)

    def operator_totals(self) -> dict:
        with self.sessions() as db:
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            buckets = list(db.scalars(select(BudgetBucket).order_by(BudgetBucket.scope_key)))
            calls = db.execute(select(ModelCall.state, func.count(), func.coalesce(func.sum(ModelCall.reserved_cost), 0),
                                      func.coalesce(func.sum(ModelCall.actual_cost), 0)).group_by(ModelCall.state)).all()
            states = {state: {"calls": int(count), "reserved_micro_usd": int(reserved) if state in {"reserved", "unknown"} else 0,
                              "actual_micro_usd": int(actual)} for state, count, reserved, actual in calls}
            return {"buckets": [{"scope_key": bucket.scope_key, "limit_cents": bucket.limit_cents,
                                  "reserved_cents": bucket.reserved_cents, "spent_cents": bucket.spent_cents} for bucket in buckets],
                    "calls_by_state": states,
                    "actual_micro_usd": sum(item["actual_micro_usd"] for item in states.values()),
                    "reserved_micro_usd": sum(item["reserved_micro_usd"] for item in states.values()),
                    "unknown_micro_usd": states.get("unknown", {}).get("reserved_micro_usd", 0)}
