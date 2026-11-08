from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from .schema import Base


class PrivacyDerivative(Base):
    """Immutable rendered bytes binding; approval is a separate exact-hash receipt."""
    __tablename__ = "privacy_derivatives"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    finding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_key: Mapped[str] = mapped_column(String(1024))
    source_sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_receipt: Mapped[str | None] = mapped_column(String(128))
    # NULL = explicit operator approval; 'demo-auto' = automated demo approval (no human review).
    approved_by: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (
        ForeignKeyConstraint(["revision_id", "finding_id", "session_id"], ["revision_findings.revision_id", "revision_findings.finding_id", "revision_findings.session_id"]),
        ForeignKeyConstraint(["finding_id", "source_key", "session_id"], ["findings.id", "findings.frame_key", "findings.session_id"]),
        CheckConstraint("object_key = 'media/' || session_id::text || '/derivatives/' || id::text", name="derivative_namespace"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND source_sha256 ~ '^[0-9a-f]{64}$'", name="hashes"),
        CheckConstraint("(approved_at IS NULL AND review_receipt IS NULL) OR (approved_at IS NOT NULL AND review_receipt = 'permission-cleared-manually-reviewed-v1')", name="review"),
    )
