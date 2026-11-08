"""Who may act on a finished inspection and its report: one rule for every post-capture action.

An inspection is managed by its owner token (the capturing tab) or by the account it is saved to,
on any device. Reading the report, its frames and audio, renaming, sharing and unlocking all resolve
the owner here, so these endpoints cannot drift apart. Capture actions (uploads, kits, skip, listing,
finalize, progress, media deletion) stay owner-token only by design: capture is bound to the device
that started it.

Outcomes follow the owner conventions: a token valid for neither an inspection nor an account is
401 TOKEN_INVALID; an owner token for another inspection, or an account the inspection is not saved
to, is 404; a deleting or deleted inspection is 404 when locked.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.contracts.enums import SessionState
from app.models import Report, Session

from .accounts import AccountService
from .auth import authenticate
from .problems import ProblemError, not_found


@dataclass(frozen=True, slots=True)
class OwnerAuthority:
    session_id: UUID
    via: Literal["owner_token", "account"]


def resolve_authority(db: DBSession, token: str, accounts: AccountService, *,
                      session_id: UUID | None = None, slug: str | None = None) -> OwnerAuthority:
    """The inspection this bearer may manage, addressed by `session_id` or by report `slug`, and how."""
    if (session_id is None) == (slug is None):
        raise ValueError("address the inspection by exactly one of session_id or slug")
    target = session_id if session_id is not None else db.scalar(
        select(Report.session_id).where(Report.share_slug == slug))
    try:
        owner_id = authenticate(db, token)
    except ProblemError:
        account = accounts.verify_token(token)  # 401 TOKEN_INVALID when it is not an account token either
        owner = None if target is None else db.get(Session, target)
        if owner is None or owner.account_id != account.id:
            raise not_found()
        return OwnerAuthority(owner.id, "account")
    if target is None or owner_id != target:
        raise not_found()
    return OwnerAuthority(owner_id, "owner_token")


def resolve_owner(db: DBSession, token: str, accounts: AccountService, *,
                  session_id: UUID | None = None, slug: str | None = None) -> UUID:
    """The id of the inspection this bearer may manage (see resolve_authority)."""
    return resolve_authority(db, token, accounts, session_id=session_id, slug=slug).session_id


def lock_owner(db: DBSession, owner_id: UUID) -> Session:
    """Lock an already-authorized inspection for a write; one being deleted is gone (404)."""
    owner = db.scalar(select(Session).where(Session.id == owner_id).with_for_update())
    if owner is None or owner.state in (SessionState.DELETING, SessionState.DELETED):
        raise not_found()
    return owner
