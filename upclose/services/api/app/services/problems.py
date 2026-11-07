"""Shared non-secret HTTP failures for lifecycle services."""
from dataclasses import dataclass
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass
class ProblemError(Exception):
    status: int
    code: str
    title: str
    detail: str
    retry_after: int | None = None


def problem_response(error: ProblemError) -> JSONResponse:
    return JSONResponse(status_code=error.status, media_type="application/problem+json",
        content={"type": "about:blank", "title": error.title, "status": error.status,
                 "code": error.code, "detail": error.detail, "request_id": str(uuid4())},
        headers={"Retry-After": str(error.retry_after)} if error.retry_after else None)


async def handle_problem(request: Request, error: ProblemError) -> JSONResponse:
    return problem_response(error)


def conflict() -> ProblemError:
    return ProblemError(409, "STATE_CONFLICT", "State conflict", "This operation conflicts with the current state.")


def not_found() -> ProblemError:
    return ProblemError(404, "NOT_FOUND", "Not found", "The requested resource was not found.")


def validation() -> ProblemError:
    return ProblemError(422, "VALIDATION", "Invalid request", "The request does not match the supported contract.")
