"""Bounded SAM smoke CLI. Fixture/dry-run by default; no provider calls without live opt-in.

Usage (fixture, zero provider calls):
    python scripts/sam_smoke.py [--output receipt.json]

Live requires ALL of: --live --confirm-live, env UPCLOSE_SMOKE_ALLOW_LIVE=1,
a leased job (--session-id/--job-id/--lease-token), private media coordinates,
and worker live settings (MODEL_MODE=live etc.). Dispatch goes through
BudgetService via run_sam_request; this script never calls transport directly.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.smoke import LIVE_ENV_OPT_IN, SmokeConfig, SmokeError, failed_receipt, run_smoke


def _write(path: Path | None, receipt: dict) -> None:
    text = json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if path is None:
        print(text, end="")
    else:
        path.write_text(text, encoding="utf-8")


def _live_dependencies(args):  # noqa: ANN001, ANN202 - lazy live-only construction
    """Build the real budget/lease/segmenter path. Only called after live opt-in."""
    from datetime import datetime, timedelta, timezone
    from uuid import UUID

    from app.adapters.meta.transport import MetaSamSegmenter
    from app.adapters.meta.types import FrameRef, MediaRef
    from app.adapters.storage import S3Storage
    from app.jobs.queue import Lease
    from app.models.database import create_session_factory
    from app.pipeline.orchestration import load_pipeline_config
    from app.services.budgets import BudgetService, budget_config_from_settings
    from app.settings import Settings

    settings = Settings()
    if settings.model_mode != "live" or settings.model_api_key is None:
        raise SmokeError("live_not_configured")
    factory = create_session_factory(settings.database_url.get_secret_value())
    budget_service = BudgetService(factory, budget_config_from_settings(settings))  # type: ignore[arg-type]
    texts = {row.prompt_id: row.text for row in load_pipeline_config().prompts.prompts}
    texts.setdefault("vehicle", "car")
    segmenter = MetaSamSegmenter(api_key=settings.model_api_key.get_secret_value(),
                                 api_base=settings.meta_api_base, storage=S3Storage(settings),
                                 prompt_texts=texts)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=args.timeout_seconds)
    lease = Lease(job_id=UUID(args.job_id), session_id=UUID(args.session_id),
                  token=UUID(args.lease_token), attempt=args.attempt, stage="segment",
                  subject_kind="concept_run", subject_id=str(args.job_id),
                  lease_until=deadline, deadline=deadline)
    media = MediaRef(media_key=args.media_key, kind=args.media_kind,
                     stored_width=args.stored_width, stored_height=args.stored_height,
                     frames=(FrameRef(frame_index=0, timestamp_ms=0,
                                      frame_key=args.frame_key or args.media_key),))
    return budget_service, segmenter, lease, media


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="request live mode (also needs --confirm-live and env opt-in)")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--prompt-id", default="vehicle")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--session-id")
    parser.add_argument("--job-id")
    parser.add_argument("--lease-token")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--media-key")
    parser.add_argument("--media-kind", choices=("image", "video"), default="image")
    parser.add_argument("--frame-key")
    parser.add_argument("--stored-width", type=int, default=640)
    parser.add_argument("--stored-height", type=int, default=480)
    args = parser.parse_args()
    try:
        config = SmokeConfig(mode="live" if args.live else "fixture",
                             max_requests=args.max_requests,
                             timeout_seconds=args.timeout_seconds, prompt_id=args.prompt_id)
    except SmokeError as error:
        print(f"SAM_SMOKE_FAILED_CLOSED: {error.code}", file=sys.stderr)
        return 2
    if config.mode == "fixture":
        receipt = run_smoke(config)
        _write(args.output, receipt)
        print("Fixture dry-run receipt written; zero provider calls.", file=sys.stderr)
        return 0
    allow_live = args.confirm_live and os.environ.get(LIVE_ENV_OPT_IN) == "1"
    try:
        if not allow_live:
            raise SmokeError("live_not_opted_in")
        if not (args.session_id and args.job_id and args.lease_token and args.media_key):
            raise SmokeError("budget_not_reserved")
        budget_service, segmenter, lease, media = _live_dependencies(args)
        receipt = run_smoke(config, budget_service=budget_service, segmenter=segmenter,
                            lease=lease, media=media, allow_live=True)
    except SmokeError as error:
        _write(args.output, failed_receipt(config, error.code))
        print(f"SAM_SMOKE_FAILED_CLOSED: {error.code}", file=sys.stderr)
        return 2
    except Exception:  # construction/validation details may carry private input; emit code only
        _write(args.output, failed_receipt(config, "live_not_configured"))
        print("SAM_SMOKE_FAILED_CLOSED: live_not_configured", file=sys.stderr)
        return 2
    _write(args.output, receipt)
    return 0 if receipt["outcome"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
