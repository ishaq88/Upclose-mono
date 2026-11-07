from uuid import uuid4
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.email import EmailSender
from app.adapters.storage import ObjectStorage, S3Storage
from app.adapters.session_storage import fenced_storage
from app.adapters.stripe import StripeGateway, build_gateway
from app.models import create_session_factory
from app.readiness import DatabaseProbe, database_ready, storage_config_ready
from app.routers.accounts import create_account_router
from app.routers.billing import create_billing_router
from app.routers.progress import create_progress_router
from app.routers.reports import create_report_router
from app.routers.privacy import create_privacy_router
from app.routers.deletion import create_deletion_router
from app.routers.sessions import create_session_router
from app.routers.operator import create_operator_router
from app.routers.uploads import create_upload_router
from app.routers.video_uploads import create_video_upload_router
from app.services.problems import ProblemError, handle_problem, problem_response, validation
from app.settings import Settings


def create_app(
    settings: Settings | None = None, *, database_probe: DatabaseProbe = database_ready,
    session_factory: sessionmaker[DBSession] | None = None, storage: ObjectStorage | None = None,
    payments: StripeGateway | None = None, email_sender: EmailSender | None = None,
) -> FastAPI:
    config = settings or Settings()
    if config.model_api_key is not None:
        raise ValueError("API process must not receive MODEL_API_KEY (worker-only); use infra/compose.live-demo.yaml on the worker only")
    application = FastAPI(title="UpClose API", version="0.1.0")
    sessions = session_factory or create_session_factory(config.database_url.get_secret_value())
    object_storage = fenced_storage(sessions, storage or S3Storage(config))
    application.include_router(create_account_router(sessions, config, email_sender))
    application.include_router(create_session_router(sessions, config))
    application.include_router(create_progress_router(sessions, config))
    application.include_router(create_report_router(sessions, config, object_storage))
    application.include_router(create_upload_router(sessions, config, object_storage))
    application.include_router(create_video_upload_router(sessions, config, object_storage))
    application.include_router(create_privacy_router(sessions, config, object_storage))
    application.include_router(create_deletion_router(sessions, config))
    application.include_router(create_billing_router(sessions, config, payments or build_gateway(
        config.payments_mode, config.stripe_secret_key.get_secret_value() if config.stripe_secret_key else None)))
    application.include_router(create_operator_router(sessions,
        config.operator_token.get_secret_value() if config.operator_token else None))
    application.add_exception_handler(ProblemError, handle_problem)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic error inputs can contain private text or bearer credentials.
        return problem_response(validation())

    @application.exception_handler(SQLAlchemyError)
    async def database_failure(request: Request, error: SQLAlchemyError) -> JSONResponse:
        return problem_response(ProblemError(503, "INTERNAL", "Service unavailable",
                                            "The service is temporarily unavailable."))

    @application.middleware("http")
    async def private_responses(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/r" or request.url.path.startswith(("/r/", "/api/v1/reports/")):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready", response_model=None)
    def ready() -> dict[str, str] | JSONResponse:
        try:
            available = storage_config_ready(config) and database_probe(config)
        except Exception:
            # Never expose connection strings, credentials, or driver payloads.
            available = False
        if not available:
            return JSONResponse(
                status_code=503,
                media_type="application/problem+json",
                content={
                    "type": "about:blank",
                    "title": "Service unavailable",
                    "status": 503,
                    "code": "INTERNAL",
                    "detail": "Database, migrations, or storage configuration is not ready.",
                    "request_id": str(uuid4()),
                },
            )
        return {"status": "ready"}

    return application


app = create_app()
