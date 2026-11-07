"""Source-keyed limits cannot use owner-bound request replay records."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .schema import Base


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    source_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default="0")
    __table_args__ = (CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="source_hash"),
                      CheckConstraint("count >= 0", name="nonnegative_count"))
