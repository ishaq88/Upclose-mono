"""Fixture-only runner; cards register domain handlers through `build_registry`.

Installed so far: ingest, fixture segment/verify, the report stages (claims, score, narrate,
render), billing intake and media purge. The read_dash handler does not exist yet.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Thread
from uuid import uuid4

from app.models.database import create_session_factory
from app.settings import Settings

from .queue import SUBJECTS, Failure, JobQueue, JobSpec, Lease, Publisher, Stage


@dataclass(frozen=True, slots=True)
class Publication:
    publish: Publisher | None = None
    downstream: tuple[JobSpec, ...] = ()


Handler = Callable[[Lease], Publication]


class StageRegistry:
    def __init__(self) -> None:
        self._handlers: dict[Stage, Handler] = {}

    def register(self, stage: Stage, handler: Handler) -> None:
        if stage not in SUBJECTS or stage in self._handlers:
            raise ValueError("invalid or already registered stage")
        self._handlers[stage] = handler

    @property
    def stages(self) -> tuple[Stage, ...]:
        return tuple(self._handlers)

    def handler(self, stage: Stage) -> Handler:
        return self._handlers[stage]


class WorkerMustRestart(RuntimeError):
    """An unfinished handler must not coexist with another claim in this process."""


class StageFailure(Exception):
    """A handler may report only a validated sanitized queue failure."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


def claimable_stages(registry: StageRegistry, allow: str = "", exclude: str = "") -> tuple[Stage, ...]:
    """ Stages this runner may claim: installed stages narrowed by WORKER_STAGES / WORKER_EXCLUDE_STAGES."""
    allowed = tuple(part.strip() for part in allow.split(",") if part.strip())
    excluded = tuple(part.strip() for part in exclude.split(",") if part.strip())
    if allowed and excluded:
        raise ValueError("set only one of WORKER_STAGES and WORKER_EXCLUDE_STAGES")
    unknown = [name for name in (*allowed, *excluded) if name not in SUBJECTS]
    if unknown:
        raise ValueError("unknown worker stage: " + ", ".join(unknown))
    return tuple(stage for stage in registry.stages if (stage in allowed if allowed else stage not in excluded))


def run_once(queue: JobQueue, registry: StageRegistry, worker_id: str, stages: Iterable[Stage] | None = None,
             after_complete: Callable[[Lease], None] | None = None) -> bool:
    claimable = tuple(registry.stages if stages is None else stages)
    if not claimable:
        return False
    lease = queue.claim(worker_id, claimable)
    if lease is None:
        return False
    results: Queue[Publication | Exception] = Queue(maxsize=1)

    def execute() -> None:
        try:
            results.put(registry.handler(lease.stage)(lease))
        except Exception as error:
            results.put(error)

    # Timed-out or lost-lease results never publish. Each handler must also bound
    # its transport/subprocess work to lease.deadline; Python cannot kill threads.
    thread = Thread(target=execute, daemon=True)
    thread.start()
    started = time.monotonic()
    next_heartbeat = started + queue.policy.heartbeat_seconds
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= queue.policy.deadline_seconds:
            raise WorkerMustRestart("Stage deadline reached; worker process must restart.")
        timeout = min(max(next_heartbeat - time.monotonic(), 0), queue.policy.deadline_seconds - elapsed)
        try:
            result = results.get(timeout=timeout)
        except Empty:
            if not queue.heartbeat(lease):
                if thread.is_alive():
                    raise WorkerMustRestart("Lease lost with handler active; worker process must restart.")
                return True
            next_heartbeat = time.monotonic() + queue.policy.heartbeat_seconds
            continue
        if isinstance(result, StageFailure):
            queue.fail(lease, result.failure)
        elif isinstance(result, Exception):
            queue.fail(lease, Failure("INTERNAL"))  # no exception payload is stored
        elif not isinstance(result, Publication):
            queue.fail(lease, Failure("INVALID_INPUT", retryable=False))
        else:
            try:
                done = queue.complete(lease, result.publish, result.downstream)
            except Exception:
                done = False
                queue.fail(lease, Failure("INTERNAL"))
            if done and after_complete is not None:
                try:
                    after_complete(lease)
                except Exception:
                    logging.warning("Post-completion hook failed for stage %s.", lease.stage)
        return True


def upload_sweeper(settings: Settings, factory: object) -> Callable[[], None]:
    """Periodic upload reconciliation in the existing loop: bounded, no extra threads."""
    from app.adapters.storage import S3Storage
    from app.services.upload_reconciler import UploadReconciler
    from app.services.video_uploads import VideoUploadReconciler

    reconciler = UploadReconciler(factory, settings, S3Storage(settings))  # type: ignore[arg-type]
    video_reconciler = VideoUploadReconciler(factory, settings, S3Storage(settings))  # type: ignore[arg-type]
    next_run = time.monotonic()

    def maybe_sweep() -> None:
        nonlocal next_run
        if time.monotonic() < next_run:
            return
        next_run = time.monotonic() + settings.upload_reconcile_interval_seconds
        try:
            result = reconciler.sweep()
            if result.outcomes:
                logging.info("Upload reconciliation: %s", {k.value: v for k, v in result.outcomes.items()})
        except Exception:
            logging.exception("Upload reconciliation failed")  # retried next interval; no payloads logged
        try:
            video_reconciler.sweep()
        except Exception:
            logging.warning("Source video reconciliation failed; retrying next interval.")

    return maybe_sweep


def live_configuration_error(settings: Settings) -> str | None:
    """First missing live gate, named without key/receipt/payload text. None means ready."""
    from app.services.budgets import PRICE_VERSION

    if settings.model_api_key is None:
        return "MODEL_API_KEY is required (worker only)"
    if not settings.meta_api_base.startswith("https://"):
        return "META_API_BASE must be an https URL"
    if settings.model_price_version != PRICE_VERSION:
        return "MODEL_PRICE_VERSION is unverified"
    if not settings.model_standard_access_receipt and not settings.model_allow_non_standard_demo:
        return "Standard receipt or explicit non-Standard demo acknowledgement is required"
    if settings.model_session_budget_cents <= 0 or settings.model_daily_budget_cents <= 0:
        return "positive MODEL_SESSION_BUDGET_CENTS / MODEL_DAILY_BUDGET_CENTS are required"
    return None


def build_registry(settings: Settings, factory: object) -> StageRegistry:
    """Install the domain handlers that exist so far: ingest (with T05b fan-out), fixture segment/verify,
    the offline T07b report stages (claims..render) and billing intake."""
    from app.adapters.storage import S3Storage
    from app.adapters.stripe import build_gateway
    from app.pipeline.orchestration import install_pipeline_handlers
    from app.pipeline.report_stages import install_report_handlers
    from app.services.billing import register_billing_handlers
    from app.pipeline.purge import register_purge_handlers
    from app.pipeline.source_video import register_source_video_handlers
    from app.pipeline.audio_ingest import register_audio_handlers

    registry = StageRegistry()
    budget = None  # set in live mode; without it every live auxiliary provider stays closed
    if settings.model_mode == "live":
        error = live_configuration_error(settings)
        if error is not None:
            raise SystemExit("Live worker refused: " + error)
        from app.adapters.meta.transport import MetaSamSegmenter
        from app.pipeline.orchestration import masked_orchestration_context, load_pipeline_config
        from app.adapters.meta.verifier import MetaSparkVerifier
        from app.pipeline.verify import SparkRawVerifier
        from app.services.budgets import BudgetService, budget_config_from_settings

        budget = BudgetService(factory, budget_config_from_settings(settings))  # type: ignore[arg-type]
        source_ctx = masked_orchestration_context(live=True, spark_verifier=True)
        texts = {row.prompt_id: row.text for row in load_pipeline_config().prompts.prompts}
        texts["vehicle"] = "car"  # Group A; all damage prompts use its committed masked inputs
        key = settings.model_api_key.get_secret_value() if settings.model_api_key else ""

        def live_factory(part_no: int, _key: str = key, _texts: dict[str, str] = texts):  # type: ignore[no-untyped-def]
            return MetaSamSegmenter(api_key=_key, api_base=settings.meta_api_base,
                                    storage=S3Storage(settings), prompt_texts=_texts)

        install_pipeline_handlers(registry, factory, S3Storage(settings), settings,
                                  source_ctx, segmenter_factory=live_factory,
                                  verifier=SparkRawVerifier(MetaSparkVerifier(api_key=key, storage=S3Storage(settings)), budget),
                                  budget_service=budget)
    else:
        from app.pipeline.orchestration import masked_orchestration_context

        source_ctx = masked_orchestration_context()
        install_pipeline_handlers(registry, factory, S3Storage(settings), settings, source_ctx)
    from app.pipeline.orchestration import plan_downstream

    register_source_video_handlers(registry, factory, S3Storage(settings), settings,  # type: ignore[arg-type]
                                   fanout=lambda outcome: plan_downstream(source_ctx, outcome))
    register_audio_handlers(registry, factory, S3Storage(settings), settings)  # type: ignore[arg-type]
    install_report_handlers(registry, factory, settings, storage=S3Storage(settings), budget_service=budget)  # type: ignore[arg-type]
    from app.briefing.voice_guide import VoiceGuide
    from app.pipeline.voice_prewarm import register_prewarm_handler

    register_prewarm_handler(registry, factory, budget, lambda: VoiceGuide(S3Storage(settings)))  # type: ignore[arg-type]
    secret = settings.stripe_secret_key.get_secret_value() if settings.stripe_secret_key else None
    register_billing_handlers(registry, factory, build_gateway(settings.payments_mode, secret))  # type: ignore[arg-type]
    register_purge_handlers(registry, factory, S3Storage(settings), settings)  # type: ignore[arg-type]
    return registry


def demo_derivative_hook(settings: Settings, factory: object) -> Callable[[Lease], None] | None:
    """ Demo-only: after a report render commits, auto-approve its evidence derivatives (opt-in, fixture payments)."""
    if not settings.demo_auto_approve_derivatives:
        return None
    from app.adapters.storage import S3Storage
    from app.services.privacy import PrivacyService

    service = PrivacyService(factory, S3Storage(settings))  # type: ignore[arg-type]

    def hook(lease: Lease) -> None:
        if lease.stage == "render" and lease.session_id is not None:
            service.auto_approve_session(settings, lease.session_id)

    return hook


def main() -> None:
    settings = Settings()
    if settings.model_mode == "replay":
        raise SystemExit("Queue runner does not support MODEL_MODE=replay.")
    if settings.model_mode == "live":
        _live_error = live_configuration_error(settings)
        if _live_error is not None:
            raise SystemExit("Live worker refused: " + _live_error)
    elif settings.model_api_key is not None:
        raise SystemExit("MODEL_API_KEY present in fixture worker; use the live-demo overlay for live mode.")
    stopped = Event()

    def stop(signum: int, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logging.basicConfig(level=logging.INFO)
    factory = create_session_factory(settings.database_url.get_secret_value())
    queue = JobQueue(factory)
    maybe_sweep = upload_sweeper(settings, factory)
    from app.pipeline.ingest import sweep_ingest_scratch
    from app.pipeline.reconcile import pipeline_sweeper
    from app.services.deletion import sweep_deleted_media, sweep_purges, sweep_retention
    from app.adapters.storage import S3Storage

    maybe_reconcile = pipeline_sweeper(settings, factory)  # type: ignore[arg-type]

    sweep_ingest_scratch(settings)  # scratch left by a crashed previous process
    registry = build_registry(settings, factory)
    try:
        claim_stages = claimable_stages(registry, settings.worker_stages, settings.worker_exclude_stages)
    except ValueError as error:
        raise SystemExit("Worker refused: " + str(error)) from None
    after_complete = demo_derivative_hook(settings, factory)
    worker_id = f"{settings.model_mode}-{uuid4().hex}"
    logging.info("Queue runner started (mode=%s); installed stages: %s; claiming: %s", settings.model_mode, ", ".join(registry.stages), ", ".join(claim_stages))
    next_purge_sweep = time.monotonic()
    tombstone_cursor = None
    audit_storage = S3Storage(settings)
    while not stopped.is_set():
        maybe_sweep()
        maybe_reconcile()
        if time.monotonic() >= next_purge_sweep:
            next_purge_sweep = time.monotonic() + 60
            try:
                sweep_retention(factory, demo_session_id=settings.demo_session_id)
                sweep_purges(factory)
                audit = sweep_deleted_media(factory, audit_storage, after=tombstone_cursor)
                tombstone_cursor = audit.cursor
            except Exception:
                logging.warning("Purge recovery sweep failed; retrying next interval.")
        if not run_once(queue, registry, worker_id, claim_stages, after_complete):
            stopped.wait(1)


if __name__ == "__main__":
    # `python -m` loads this file as `__main__`, a second copy whose Publication and
    # StageFailure classes differ from those handler modules import. Run the canonical
    # module so result isinstance checks match.
    import app.jobs.worker as canonical

    canonical.main()
