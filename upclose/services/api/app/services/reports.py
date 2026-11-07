"""Current-revision report access and one scope-filtered serialization boundary."""
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.enums import ReportState, SessionState
from app.contracts.reports import FreeReport, PaidReport, ReportStatus, ShareDisableResponse, ShareEnableResponse
from app.models import Report, ReportRevision, Session
from app.services.privacy import approved_for, PrivacyService
from app.adapters.storage import ObjectStorage, StorageError
from app.services.accounts import AccountService
from app.services.auth import authenticate, bearer_token, owner_session
from app.services.billing import has_active_entitlement
from app.services.problems import ProblemError, not_found
from app.services.snapshots import Provenance, RevisionSnapshot, build_free_report, build_paid_report, load_snapshot
from app.settings import Settings

ReportScope = Literal["free", "paid"]
ReportDTO = FreeReport | PaidReport | ReportStatus


def assemble_report(revision: RevisionSnapshot, scope: ReportScope) -> FreeReport | PaidReport:
    """Serialize only a validated frozen snapshot, never mutable finding rows."""
    if scope == "free":
        return build_free_report(revision)
    if scope == "paid":
        return build_paid_report(revision)
    raise ValueError("unsupported report scope")


@dataclass(frozen=True, slots=True)
class ReportRead:
    dto: ReportDTO
    provenance_label: Provenance | None = None


def unavailable() -> ProblemError:
    return ProblemError(503, "REPORT_NOT_READY", "Report unavailable",
                        "The report is temporarily unavailable.", retry_after=5)


class ReportService:
    def __init__(self, factory: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage | None = None) -> None:
        self.factory = factory
        self.settings = settings
        self.storage = storage
        self.accounts = AccountService(factory, settings)

    def get(self, slug: str, authorization: str | None = None) -> ReportRead:
        with self.factory.begin() as db:
            return self._get(db, slug, authorization)

    def _get(self, db: DBSession, slug: str, authorization: str | None = None) -> ReportRead:
        # Explicit bearer authority is never silently downgraded to slug authority.
        owner_id = None
        account_id = None
        if authorization is not None:
            token = bearer_token(authorization)
            try:
                owner_id = authenticate(db, token)
            except ProblemError:
                # Account Bearer: resolves to an account, never a session.
                # Invalid for both authorities stays 401 TOKEN_INVALID.
                account_id = self.accounts.verify_token(token).id
        session_id = db.scalar(select(Report.session_id).where(Report.share_slug == slug))
        if session_id is None:
            raise not_found()
        if owner_id is not None and owner_id != session_id:
            raise not_found()
        # Session-scoped publication and deletion serialize on the owner first.
        # Entitlement lookup uses current statement state; an in-flight read
        # may precede a refund commit and issues no future media authority.
        owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        if owner is None:
            raise not_found()
        if account_id is not None and owner.account_id != account_id:
            raise not_found()
        if owner.state in (SessionState.DELETING, SessionState.DELETED) or owner.deleted_at is not None:
            raise ProblemError(410, "NOT_FOUND", "Report removed", "This report is no longer available.")
        report = db.scalar(select(Report).where(Report.share_slug == slug, Report.session_id == owner.id))
        if report is None:
            raise not_found()
        if owner_id is None and account_id is None and not (self.settings.public_sharing_enabled and report.share_enabled):
            raise not_found()
        revision = db.get(ReportRevision, report.current_revision_id) if report.current_revision_id else None
        if revision is None or revision.report_id != report.id or revision.session_id != report.session_id:
            raise unavailable()
        scope: ReportScope = "paid" if has_active_entitlement(db, report.id) else "free"
        if revision.state in (ReportState.BUILDING, ReportState.FAILED):
            return ReportRead(ReportStatus(scope=scope, slug=slug, revision_id=revision.id, state=revision.state))
        try:
            snapshot = load_snapshot(db, revision, slug)
            if (snapshot.score is None or snapshot.free_narration is None
                    or snapshot.paid_narration is None or snapshot.paid_narration.narration is None
                    or snapshot.rendered_at is None):
                raise ValueError("ready snapshot is incomplete")
            dto = assemble_report(snapshot, scope)
            dto = dto.model_copy(update={'vehicle_label': owner.vehicle_label, 'findings': tuple(f.model_copy(update={'evidence_available':
                approved_for(db, owner.id, revision.id, f.id) is not None}) for f in dto.findings)})
            if isinstance(dto, PaidReport):
                dto = dto.model_copy(update={'candidates': tuple(f.model_copy(update={'evidence_available':
                    approved_for(db, owner.id, revision.id, f.id) is not None}) for f in dto.candidates)})
        except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
            # Malformed persisted content must never escape in error details or partial output.
            raise unavailable() from None
        return ReportRead(dto, snapshot.manifest.provenance)

    def _owned_report(self, db: DBSession, slug: str, token: str) -> tuple[Session, Report]:
        # Owner token ONLY: `owner_session` authenticates the session token
        # (401 for account/invalid tokens) and locks the owner row, following
        # the write convention of 404 for deleting/deleted sessions.
        owner = owner_session(db, token, lock=True)
        report = db.scalar(select(Report).where(Report.share_slug == slug, Report.session_id == owner.id).with_for_update())
        if report is None:
            raise not_found()
        return owner, report

    def enable_share(self, slug: str, token: str) -> ShareEnableResponse:
        """Enable mechanic sharing for a ready, actively paid report (idempotent)."""
        with self.factory.begin() as db:
            _owner, report = self._owned_report(db, slug, token)
            if not has_active_entitlement(db, report.id):
                raise ProblemError(403, "SHARE_REQUIRES_PAID", "Paid report required",
                                   "Sharing is available only for an unlocked paid report.")
            revision = db.get(ReportRevision, report.current_revision_id) if report.current_revision_id else None
            if (revision is None or revision.report_id != report.id or revision.session_id != report.session_id
                    or revision.state != ReportState.READY):
                raise ProblemError(409, "REPORT_NOT_READY", "Report not ready",
                                   "The report is not ready to share yet.")
            report.share_enabled = True
            return ShareEnableResponse(url=f"/r/{report.share_slug}", share_enabled=True,
                                       public_access_active=bool(self.settings.public_sharing_enabled and report.share_enabled))

    def disable_share(self, slug: str, token: str) -> ShareDisableResponse:
        """Disable mechanic sharing (idempotent; no entitlement/readiness gate,
        so access can always be revoked, including after a refund)."""
        with self.factory.begin() as db:
            _owner, report = self._owned_report(db, slug, token)
            report.share_enabled = False
            return ShareDisableResponse(share_enabled=False)

    def frame_url(self, slug: str, finding_id: UUID, authorization: str | None = None) -> str:
        with self.factory.begin() as db:
            result = self._get(db, slug, authorization)
            if self.storage is None:
                raise not_found()
            dto = result.dto
            if isinstance(dto, ReportStatus) or not any(f.id == finding_id and f.evidence_available for f in (*dto.findings, *getattr(dto, 'candidates', ()))):
                raise not_found()
            report = db.scalar(select(Report).where(Report.share_slug == slug))
            row = approved_for(db, report.session_id, dto.revision_id, finding_id)
            if row is None:
                raise not_found()
            identity, key, digest = row.id, row.object_key, row.sha256
        PrivacyService(self.factory, self.storage).verify_bytes(key, digest)
        # Recheck authorization/revision/approval after external I/O, under owner lock.
        with self.factory.begin() as db:
            result = self._get(db, slug, authorization)
            dto = result.dto
            if isinstance(dto, ReportStatus) or not any(f.id == finding_id and f.evidence_available for f in (*dto.findings, *getattr(dto, 'candidates', ()))):
                raise not_found()
            report = db.scalar(select(Report).where(Report.share_slug == slug))
            row = approved_for(db, report.session_id, dto.revision_id, finding_id)
            if row is None or (row.id, row.object_key, row.sha256) != (identity, key, digest):
                raise not_found()
            # Presigning is local cryptography; it must not perform provider I/O.
            try:
                return self.storage.presign_read(key, expires_in=60)
            except StorageError:
                raise not_found() from None
