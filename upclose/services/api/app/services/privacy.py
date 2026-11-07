"""Private derivative creation and explicit operator review; storage I/O stays outside DB transactions."""
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker
from app.adapters.storage import ObjectStorage, StorageError
from app.contracts.enums import SessionState
from app.contracts.evidence import BoundingBox, Finding
from app.models import Artifact, PrivacyDerivative, Report, ReportRevision, Session
from app.pipeline.derivatives import MAX_IMAGE_BYTES, render_derivative
from app.services.problems import not_found, ProblemError
from app.services.snapshots import load_snapshot
from app.settings import Settings

RECEIPT = "permission-cleared-manually-reviewed-v1"


def operator_auth(settings: Settings, token: str | None) -> None:
    secret = settings.operator_token.get_secret_value() if settings.operator_token else ""
    if not secret or token is None or not hmac.compare_digest(secret.encode(), token.encode()):
        raise not_found()


def lock_owner(db: DBSession, session_id: UUID) -> Session:
    owner = db.scalar(select(Session).where(Session.id == session_id).with_for_update())
    if owner is None or owner.state in (SessionState.DELETING, SessionState.DELETED) or owner.deleted_at is not None:
        raise not_found()
    return owner


def bound_finding(db: DBSession, session_id: UUID, revision_id: UUID, finding_id: UUID) -> tuple[Finding, str]:
    lock_owner(db, session_id)
    report = db.scalar(select(Report).where(Report.session_id == session_id, Report.current_revision_id == revision_id))
    revision = db.get(ReportRevision, revision_id)
    if report is None or revision is None or revision.state != 'ready':
        raise not_found()
    snapshot = load_snapshot(db, revision, report.share_slug)
    finding = next((f for f in snapshot.manifest.all_findings if f.id == finding_id and f.status in ('verified', 'unverified')), None)
    if finding is None:
        raise not_found()
    artifact = db.scalar(select(Artifact).where(Artifact.session_id == session_id,
        Artifact.key == finding.evidence.frame_key, Artifact.kind == 'frame', Artifact.purge_state == 'retained'))
    if artifact is None:
        raise not_found()
    return finding, artifact.sha256


def approved_for(db: DBSession, session_id: UUID, revision_id: UUID, finding_id: UUID) -> PrivacyDerivative | None:
    try:
        finding, source_hash = bound_finding(db, session_id, revision_id, finding_id)
    except ProblemError:
        return None
    return db.scalar(select(PrivacyDerivative).where(PrivacyDerivative.session_id == session_id,
        PrivacyDerivative.revision_id == revision_id, PrivacyDerivative.finding_id == finding_id,
        PrivacyDerivative.source_key == finding.evidence.frame_key, PrivacyDerivative.source_sha256 == source_hash,
        PrivacyDerivative.approved_at.is_not(None), PrivacyDerivative.review_receipt == RECEIPT)
        .order_by(PrivacyDerivative.created_at.desc(), PrivacyDerivative.id))


class PrivacyService:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage):
        self.factory, self.storage = factory, storage

    def create(self, session_id: UUID, revision_id: UUID, finding_id: UUID,
               redactions: tuple[BoundingBox, ...]) -> dict[str, str]:
        with self.factory.begin() as db:
            finding, source_hash = bound_finding(db, session_id, revision_id, finding_id)
        derivative_id = uuid4()
        key = f"media/{session_id}/derivatives/{derivative_id}"
        try:
            with TemporaryDirectory(prefix='upclose-derivative-') as directory:
                source, target = Path(directory) / 'source', Path(directory) / 'derivative.png'
                downloaded = self.storage.download_to(finding.evidence.frame_key, source, MAX_IMAGE_BYTES)
                if downloaded.sha256 != source_hash:
                    raise ValueError('source hash')
                mask_path = None
                if finding.evidence.mask_key:
                    mask_path = Path(directory) / 'mask.png'
                    self.storage.download_to(finding.evidence.mask_key, mask_path, MAX_IMAGE_BYTES)
                render_derivative(source, target, width=finding.evidence.frame_width,
                    height=finding.evidence.frame_height, bbox=finding.evidence.bbox, redactions=redactions, mask=mask_path)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                self.storage.put_file(key, target, 'image/png')
            with self.factory.begin() as db:
                current, current_hash = bound_finding(db, session_id, revision_id, finding_id)
                if current != finding or current_hash != source_hash:
                    raise not_found()
                db.add(PrivacyDerivative(id=derivative_id, session_id=session_id, revision_id=revision_id,
                    finding_id=finding_id, source_key=finding.evidence.frame_key, source_sha256=source_hash,
                    object_key=key, sha256=digest))
        except (StorageError, ValueError, OSError):
            raise not_found() from None
        return {'id': str(derivative_id), 'sha256': digest, 'state': 'pending_manual_review'}

    def approve(self, derivative_id: UUID, sha256: str, receipt: str) -> dict[str, str]:
        with self.factory.begin() as db:
            row = db.get(PrivacyDerivative, derivative_id)
            if row is None or row.sha256 != sha256 or receipt != RECEIPT:
                raise not_found()
            sid, rid, fid, key = row.session_id, row.revision_id, row.finding_id, row.object_key
            bound_finding(db, sid, rid, fid)
        self.verify_bytes(key, sha256)
        with self.factory.begin() as db:
            bound_finding(db, sid, rid, fid)
            row = db.scalar(select(PrivacyDerivative).where(PrivacyDerivative.id == derivative_id).with_for_update())
            if row is None or row.sha256 != sha256:
                raise not_found()
            if row.approved_at is None:
                row.approved_at, row.review_receipt = datetime.now(timezone.utc), receipt
        return {'id': str(derivative_id), 'sha256': sha256, 'state': 'approved'}

    def verify_bytes(self, key: str, sha256: str) -> None:
        try:
            with TemporaryDirectory(prefix='upclose-verify-') as directory:
                downloaded = self.storage.download_to(key, Path(directory) / 'image', MAX_IMAGE_BYTES)
                if downloaded.sha256 != sha256:
                    raise ValueError('derivative hash')
        except (StorageError, OSError, ValueError):
            raise not_found() from None

    def preview(self, derivative_id: UUID) -> bytes:
        with self.factory.begin() as db:
            row = db.get(PrivacyDerivative, derivative_id)
            if row is None:
                raise not_found()
            sid, rid, fid, key, digest = row.session_id, row.revision_id, row.finding_id, row.object_key, row.sha256
            bound_finding(db, sid, rid, fid)
        try:
            with TemporaryDirectory(prefix='upclose-preview-') as directory:
                path = Path(directory) / 'image'
                downloaded = self.storage.download_to(key, path, MAX_IMAGE_BYTES)
                if downloaded.sha256 != digest:
                    raise ValueError('derivative hash')
                data = path.read_bytes()
        except (StorageError, OSError, ValueError):
            raise not_found() from None
        with self.factory.begin() as db:
            bound_finding(db, sid, rid, fid)
        return data
