"""Shared daily source limits. IP addresses are hashed before storage."""
import hashlib
import ipaddress
from datetime import timedelta, timezone
from math import ceil

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession

from app.models.rate_limits import RateLimitBucket
from .problems import ProblemError


def source_fingerprint(request: Request, trusted_proxy_hosts: tuple[str, ...]) -> str:
    peer = request.client.host if request.client else "unknown"
    networks = tuple(ipaddress.ip_network(value, strict=False) for value in trusted_proxy_hosts)
    try:
        address = ipaddress.ip_address(peer)
        if any(address in network for network in networks):
            forwarded = request.headers.get("x-forwarded-for", "")
            try:
                chain = [ipaddress.ip_address(item.strip()) for item in forwarded.split(",") if item.strip()]
                for candidate in reversed(chain):
                    address = candidate
                    if not any(candidate in network for network in networks):
                        break
            except ValueError:
                address = ipaddress.ip_address(peer)
        source = str(address)
    except ValueError:
        source = "unknown"
    return hashlib.sha256(("session-create:" + source).encode()).hexdigest()


def consume_creation_limit(db: DBSession, source_hash: str, limit: int) -> None:
    now = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
    window = now.replace(hour=0, minute=0, second=0, microsecond=0)
    statement = insert(RateLimitBucket).values(source_hash=source_hash, window_start=window, count=1)
    statement = statement.on_conflict_do_update(
        index_elements=[RateLimitBucket.source_hash, RateLimitBucket.window_start],
        set_={"count": RateLimitBucket.count + 1}, where=RateLimitBucket.count < limit,
    ).returning(RateLimitBucket.count)
    if db.scalar(statement) is None:
        raise ProblemError(429, "RATE_LIMITED", "Rate limit exceeded", "The daily session creation limit was reached.",
                           max(1, ceil((window + timedelta(days=1) - now).total_seconds())))
