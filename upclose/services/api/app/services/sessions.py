"""Buyer capture lifecycle. Session rows are locked before their children."""
from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import String, cast, exists, func, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.api import (CreateSessionRequest, CreateSessionResponse, FinalizeResponse,
    PartSummary, SessionResponse, SkipPartResponse, UpdateListingResponse, _clean_vehicle_label)
from app.contracts.accounts import (AccountSessionSummary, AccountSessionsResponse,
    ClaimSessionResponse)
from app.contracts.enums import PART_ZONES, PartState, SessionState, UploadState
from app.models import AudioUpload, Job, Part, Report, ReportRevision, Session, Upload, VideoUpload
from app.pipeline.orchestration import advance_session
from app.settings import Settings
from .auth import owner_session, token_hash
from .idempotency import execute_idempotent
from .problems import conflict, not_found, validation
from .rate_limits import consume_creation_limit


class SessionService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings

    def create(self, request: CreateSessionRequest, source_hash: str, *,
               account_id: UUID | None = None) -> CreateSessionResponse:
        if request.consent_version != self.settings.consent_version:
            raise validation()
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            consume_creation_limit(db, source_hash, self.settings.session_creation_limit_per_day)
            now = db.scalar(select(func.clock_timestamp()))
            owner = Session(token_hash=token_hash(token), consent_version=request.consent_version,
                            consent_at=now, listing_text=request.listing_text, capture_mode=request.capture_mode,
                            account_id=account_id)
            db.add(owner)
            db.flush()
            db.add_all([Part(session_id=owner.id, part_no=number, zone=zone)
                        for number, zone in enumerate(PART_ZONES, start=1)])
            db.flush()
            result = CreateSessionResponse(session_id=owner.id, session_token=token)
        # Creation intentionally never writes a request-key record containing the token.
        return result

    def get(self, token: str, session_id: UUID) -> SessionResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id)
            return self._response(db, owner)

    def get_for_account(self, account_id: UUID, session_id: UUID) -> SessionResponse:
        """Account read: only the linked account; anything else is a 404."""
        with self.sessions.begin() as db:
            owner = db.scalar(select(Session).where(Session.id == session_id))
            if (owner is None or owner.account_id != account_id
                    or owner.state in (SessionState.DELETING, SessionState.DELETED)):
                raise not_found()
            return self._response(db, owner)

    def claim(self, account_id: UUID, session_id: UUID, owner_token: str) -> ClaimSessionResponse:
        """Link an inspection to an account, proven by its owner token.

        A wrong owner token follows owner conventions (401 unknown token,
        404 token for another inspection); a link to another account is 409.
        Re-claiming by the same account is idempotent.
        """
        with self.sessions.begin() as db:
            owner = owner_session(db, owner_token, session_id, lock=True)
            if owner.account_id is not None and owner.account_id != account_id:
                raise conflict()
            owner.account_id = account_id
            db.flush()
            return ClaimSessionResponse(session_id=owner.id, account_id=account_id)

    def list_for_account(self, account_id: UUID) -> AccountSessionsResponse:
        """Newest-first, free-safe summaries of only this account's inspections."""
        with self.sessions.begin() as db:
            owners = db.scalars(select(Session).where(
                Session.account_id == account_id,
                Session.state.notin_([SessionState.DELETING, SessionState.DELETED]),
            ).order_by(Session.created_at.desc(), Session.id.desc())).all()
            summaries = []
            for owner in owners:
                counts: dict[str, int] = {}
                for state, count in db.execute(select(Part.state, func.count()).where(
                        Part.session_id == owner.id).group_by(Part.state)).all():
                    counts[state.value] = count
                report = db.scalar(select(Report).where(Report.session_id == owner.id))
                report_state = None
                if report is not None and report.current_revision_id is not None:
                    revision = db.get(ReportRevision, report.current_revision_id)
                    if revision is not None and revision.report_id == report.id:
                        report_state = revision.state
                summaries.append(AccountSessionSummary(
                    session_id=owner.id, state=owner.state, purpose=owner.purpose,
                    capture_mode=owner.capture_mode, created_at=owner.created_at,
                    updated_at=owner.updated_at, part_state_counts=counts,
                    report_state=report_state,
                    report_slug=report.share_slug if report is not None else None,
                    vehicle_label=owner.vehicle_label))
            return AccountSessionsResponse(sessions=tuple(summaries))

    @staticmethod
    def _response(db: DBSession, owner: Session) -> SessionResponse:
            parts = db.scalars(select(Part).where(Part.session_id == owner.id).order_by(Part.part_no)).all()
            return SessionResponse(session_id=owner.id, purpose=owner.purpose, state=owner.state, capture_mode=owner.capture_mode,
                consent_at=owner.consent_at, listing_text=owner.listing_text, listing_version=owner.listing_version,
                vehicle_label=owner.vehicle_label,
                parts=tuple(PartSummary(id=part.id, part_no=part.part_no, zone=part.zone, state=part.state,
                                       reason=part.failure_reason, active_upload_id=part.active_upload_id)
                            for part in parts))

    def update_label(self, token: str, session_id: UUID, label: str) -> SessionResponse:
        try:
            cleaned = _clean_vehicle_label(label)
        except ValueError:
            raise validation() from None
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            # Display name only: settable while capturing and after finalize;
            # never touches listing_text/listing_version. Deleting/deleted
            # sessions already 404 via owner_session.
            owner.vehicle_label = cleaned
            db.flush()
            return self._response(db, owner)

    def update_listing(self, token: str, session_id: UUID, listing_text: str,
                       idempotency_key: str | None) -> UpdateListingResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            self._capturing(owner)
            def change() -> dict[str, object]:
                self._capturing(owner)
                if owner.listing_text != listing_text:
                    owner.listing_text = listing_text
                    owner.listing_version += 1
                db.flush()
                return {"listing_version": owner.listing_version}
            if idempotency_key is None:
                result = change()
            else:
                result = execute_idempotent(db, authority_id=owner.id, route=f"/sessions/{owner.id}/listing",
                    key=idempotency_key, body={"listing_text": listing_text}, operation=change)
            return UpdateListingResponse.model_validate(result)

    def skip(self, token: str, part_id: UUID) -> SkipPartResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            part = db.scalar(select(Part).where(Part.id == part_id, Part.session_id == owner.id).with_for_update())
            if part is None:
                raise not_found()
            if part.state == PartState.SKIPPED:
                return SkipPartResponse()
            self._capturing(owner)
            if part.state != PartState.PENDING or part.source_video_id is not None:
                raise conflict()
            active = db.scalar(select(Upload.id).where(Upload.part_id == part.id,
                Upload.state.in_([UploadState.OPEN, UploadState.COMPLETING])).with_for_update())
            if active:
                raise conflict()
            part.state = PartState.SKIPPED
            return SkipPartResponse()

    def finalize(self, token: str, session_id: UUID) -> FinalizeResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            if owner.state == SessionState.FINALIZED:
                report = db.scalar(select(Report).where(Report.session_id == owner.id))
                if report is None:
                    raise conflict()
                return FinalizeResponse(report_slug=report.share_slug)
            self._capturing(owner)
            parts = db.scalars(select(Part).where(Part.session_id == owner.id).order_by(Part.part_no).with_for_update()).all()
            active = db.scalar(select(Upload.id).where(Upload.session_id == owner.id,
                Upload.state.in_([UploadState.OPEN, UploadState.COMPLETING])).limit(1).with_for_update())
            source_active = db.scalar(select(VideoUpload.id).where(VideoUpload.session_id == owner.id,
                (VideoUpload.state.in_([UploadState.OPEN, UploadState.COMPLETING])) |
                (VideoUpload.processing_state == "queued")).limit(1))
            audio_active = db.scalar(select(AudioUpload.id).where(AudioUpload.session_id == owner.id,
                (AudioUpload.state == UploadState.OPEN) | ((AudioUpload.state == UploadState.ACCEPTED) &
                 (AudioUpload.processing_state == "queued") &
                 exists().where(Job.session_id == AudioUpload.session_id, Job.stage == "audio_ingest",
                                Job.subject_id == cast(AudioUpload.id, String),
                                Job.state.in_(("queued", "running"))))).limit(1))
            if (active is not None or source_active is not None or audio_active is not None
                    or any(part.state == PartState.UPLOADING for part in parts)):
                raise conflict()
            for part in parts:
                if part.state == PartState.PENDING:
                    part.state = PartState.SKIPPED
                    if owner.capture_mode == "single_video":
                        part.failure_reason = "VIDEO_ZONE_UNCONFIRMED"
            owner.state = SessionState.FINALIZED
            owner.finalized_at = db.scalar(select(func.clock_timestamp()))
            report = Report(session_id=owner.id, share_slug=secrets.token_urlsafe(24))
            db.add(report)
            db.flush()
            # This captures selection only; T07b builds the immutable ready evidence snapshot.
            selection = {"session_id": str(owner.id), "listing_version": owner.listing_version,
                         "parts": [{"part_id": str(part.id), "part_no": part.part_no,
                                    "state": part.state.value} for part in parts]}
            revision = ReportRevision(session_id=owner.id, report_id=report.id, revision_no=1,
                                      manifest={"finalized_selection": selection})
            db.add(revision)
            db.flush()
            report.current_revision_id = revision.id
            db.flush()
            advance_session(db, owner.id)  # every selected part may already be terminal: schedule the report chain once
            return FinalizeResponse(report_slug=report.share_slug)

    @staticmethod
    def _capturing(owner: Session) -> None:
        if owner.state != SessionState.CAPTURING:
            raise conflict()
