"""Account wire contracts (T15b). Tokens appear only in AuthResponse, once."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .base import ContractModel, Count, UTCDateTime
from .enums import ReportState, SessionState


class RegisterRequest(ContractModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class LoginRequest(ContractModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=1024)]


class ChangePasswordRequest(ContractModel):
    current_password: Annotated[str, Field(min_length=1, max_length=1024)]
    new_password: Annotated[str, Field(min_length=1, max_length=1024)]


class PasswordResetRequest(ContractModel):
    # Deliberately permissive: malformed addresses get the same 202 as
    # unknown ones, so the endpoint cannot enumerate accounts.
    email: Annotated[str, Field(max_length=320)]


class PasswordResetConfirmRequest(ContractModel):
    # Length/format failures map to the generic reset_token_invalid problem
    # in the service, not a distinguishing contract error.
    token: Annotated[str, Field(max_length=1024)]
    new_password: Annotated[str, Field(min_length=1, max_length=1024)]


class AccountSummary(ContractModel):
    id: UUID
    email: Annotated[str, Field(min_length=3, max_length=320)]
    created_at: UTCDateTime
    disabled: bool = False


class AuthResponse(ContractModel):
    """The plaintext token is returned exactly once, at issuance."""

    account: AccountSummary
    token: Annotated[str, Field(min_length=22, max_length=256)]
    expires_at: UTCDateTime


class ClaimSessionRequest(ContractModel):
    session_id: UUID
    owner_token: Annotated[str, Field(min_length=22, max_length=256)]


class ClaimSessionResponse(ContractModel):
    session_id: UUID
    account_id: UUID


class AccountSessionSummary(ContractModel):
    """Free-safe account listing row: no findings, costs, evidence, or tokens."""

    session_id: UUID
    state: SessionState
    purpose: Literal["buyer"]
    capture_mode: Literal["guided", "single_video"] = "guided"
    created_at: UTCDateTime
    updated_at: UTCDateTime
    part_state_counts: dict[str, Count]
    report_state: ReportState | None = None
    report_slug: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    vehicle_label: Annotated[str, Field(min_length=1, max_length=80)] | None = None


class AccountSessionsResponse(ContractModel):
    sessions: tuple[AccountSessionSummary, ...]
