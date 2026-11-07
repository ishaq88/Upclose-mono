"""Live-demo wiring checks with mocked transport. No network, keys, or database."""
import base64
import hashlib
import json
import urllib.error
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.adapters.meta import MediaRef, ProviderFailure
from app.adapters.meta.transport import MetaSamSegmenter, urllib_transport
from app.adapters.storage import StorageError
from app.jobs.worker import live_configuration_error
from app.pipeline.verify import LivePendingVerifier
from app.services.budgets import BudgetConfig, BudgetError, BudgetService, ReservationRequest, UsageUnits, budget_config_from_settings
from app.settings import Settings

TEST_KEY = "test-key-for-tests"
REQUEST = UUID("00000000-0000-4000-8000-000000000001")
IMAGE_OUTPUT = json.loads((Path(__file__).parent / "fixtures/meta/responses.json").read_text(encoding="utf-8"))["image"]["output_text"]


@dataclass
class Stored:
    size_bytes: int


class MemoryStorage:
    def __init__(self, payload: bytes = b"fake-media") -> None:
        self.payload = payload

    def download_to(self, key: str, path: Path, max_bytes: int) -> Stored:
        path.write_bytes(self.payload)
        return Stored(len(self.payload))


class BrokenStorage:
    def download_to(self, key: str, path: Path, max_bytes: int) -> Stored:
        raise StorageError("404")


def image_media() -> MediaRef:
    return MediaRef(media_key="media/test/frame.jpg", kind="image", stored_width=4, stored_height=3,
                    frames=[{"frame_index": 0, "timestamp_ms": 0, "frame_key": "media/test/frame.jpg"}])


def completed(text: str, **usage: int) -> tuple[int, bytes]:
    body = {"model": "sam-3.1", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": usage}
    return 200, json.dumps(body).encode()


def segmenter(transport, storage=None) -> MetaSamSegmenter:
    return MetaSamSegmenter(api_key=TEST_KEY, api_base="https://api.meta.ai",
                            storage=storage or MemoryStorage(), prompt_texts={"rust_a": "rust"},
                            transport=transport)


def test_live_image_success_is_live_provenance_with_bearer_key() -> None:
    seen: dict[str, object] = {}

    def fake(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        seen.update(url=url, headers=headers, body=json.loads(body))
        return completed(IMAGE_OUTPUT, images_processed=1)

    result = segmenter(fake).segment(image_media(), "rust_a", REQUEST)
    assert result.provenance == "live"
    assert len(result.observations) == 1
    assert result.usage.images_processed == 1
    assert seen["url"] == "https://api.meta.ai/v1/responses"
    assert seen["headers"]["Authorization"] == "Bearer " + TEST_KEY
    content = seen["body"]["input"][0]["content"]  # type: ignore[index]
    assert content[0] == {"type": "input_text", "text": "rust"}
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(content[1]["image_url"].split(",", 1)[1]) == b"fake-media"


def test_live_empty_completed_is_zero_candidates() -> None:
    result = segmenter(lambda *a: completed("", images_processed=1)).segment(image_media(), "rust_a", REQUEST)
    assert result.observations == ()


def test_masked_png_mime_and_hash_are_bound_to_submitted_bytes() -> None:
    payload = b"\x89PNG\r\n\x1a\nsynthetic-test-bytes"
    media = image_media().model_copy(update={"content_sha256": hashlib.sha256(payload).hexdigest()})
    captured = []

    def transport(url, body, headers, timeout):
        captured.append(json.loads(body)["input"][0]["content"][1]["image_url"])
        return completed("", images_processed=1)

    segmenter(transport, MemoryStorage(payload)).segment(media, "rust_a", REQUEST)
    assert captured == ["data:image/png;base64," + base64.b64encode(payload).decode()]
    with pytest.raises(ProviderFailure, match="malformed_record"):
        segmenter(transport, MemoryStorage(payload + b"changed")).segment(media, "rust_a", REQUEST)
    assert len(captured) == 1  # changed bytes never reach the provider


@pytest.mark.parametrize("status,code", [(401, "standard_access_unverified"), (403, "standard_access_unverified"),
                                         (429, "provider_timeout"), (500, "provider_timeout"), (503, "provider_timeout"), (400, "provider_failed")])
def test_http_failures_are_sanitized_codes(status: int, code: str) -> None:
    with pytest.raises(ProviderFailure, match=code) as caught:
        segmenter(lambda *a: (status, b"{}")).segment(image_media(), "rust_a", REQUEST)
    assert caught.value.code == code
    assert caught.value.http_status == status
    assert caught.value.http_status_class == f"{status // 100}xx"


def test_provider_failure_status_is_typed_and_sanitized() -> None:
    assert ProviderFailure("provider_timeout").http_status is None
    assert ProviderFailure("provider_timeout").http_status_class is None
    assert ProviderFailure("provider_failed", http_status=999).http_status is None
    assert ProviderFailure("provider_failed", http_status=True).http_status is None  # bool is not a status
    assert ProviderFailure("provider_failed", http_status=404).http_status_class == "4xx"


def test_incomplete_wrong_model_and_malformed_fail_closed() -> None:
    with pytest.raises(ProviderFailure, match="incomplete_response"):
        segmenter(lambda *a: (200, b'{"status": "in_progress", "output": []}')).segment(image_media(), "rust_a", REQUEST)
    with pytest.raises(ProviderFailure, match="model_unverified"):
        segmenter(lambda *a: (200, b'{"model": "other", "status": "completed", "output": []}')).segment(image_media(), "rust_a", REQUEST)
    with pytest.raises(ProviderFailure, match="malformed_record"):
        segmenter(lambda *a: (200, b"not-json")).segment(image_media(), "rust_a", REQUEST)


def test_unknown_prompt_and_storage_failure_never_calls_provider() -> None:
    calls = 0

    def fake(*args: object) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        return completed("")

    with pytest.raises(ProviderFailure, match="model_unverified"):
        segmenter(fake).segment(image_media(), "unknown_prompt", REQUEST)
    assert calls == 0
    with pytest.raises(ProviderFailure, match="provider_failed"):
        segmenter(fake, BrokenStorage()).segment(image_media(), "rust_a", REQUEST)


def test_dispatch_persists_and_logs_sanitized_failure_code_and_status(caplog) -> None:
    import logging
    from datetime import timedelta
    from app.jobs.queue import Lease
    from app.pipeline.sam_dispatch import run_sam_request

    captured: dict = {}

    class FakeBudget:
        def reserve(self, request): captured["attempt_id"] = request.attempt_id
        def mark_dispatched(self, attempt_id, lease_token): return True
        def mark_unknown(self, attempt_id, *, failure_code=None, http_status=None):
            captured.update(attempt_id=attempt_id, failure_code=failure_code, http_status=http_status)
        def settle(self, attempt_id, **kwargs): raise AssertionError("failed request must not settle")

    now = datetime.now(timezone.utc)
    lease = Lease(job_id=uuid4(), session_id=uuid4(), token=uuid4(), attempt=1, stage="segment",
                  subject_kind="concept_run", subject_id="x", lease_until=now + timedelta(minutes=2),
                  deadline=now + timedelta(minutes=5))
    secret_payload = b'{"error": "PAYLOAD-SECRET-TEXT"}'
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ProviderFailure, match="provider_timeout"):
            run_sam_request(lease, segmenter(lambda *a: (503, secret_payload)), image_media(), REQUEST, "rust_a", FakeBudget())  # type: ignore[arg-type]
    assert captured["failure_code"] == "provider_timeout" and captured["http_status"] == 503
    assert "provider_timeout" in caplog.text and "503" in caplog.text and "5xx" in caplog.text
    assert TEST_KEY not in caplog.text and "PAYLOAD-SECRET-TEXT" not in caplog.text
    assert TEST_KEY not in str(captured) and "PAYLOAD-SECRET-TEXT" not in str(captured)


def test_urllib_unreachable_maps_to_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr("app.adapters.meta.transport.urllib.request.urlopen", boom)
    with pytest.raises(ProviderFailure, match="provider_timeout") as caught:
        urllib_transport("https://api.meta.ai/v1/responses", b"{}", {}, 1.0)
    assert caught.value.http_status is None and caught.value.http_status_class is None


def test_budget_config_from_settings_tunable_caps_and_demo_gate() -> None:
    demo = Settings(model_mode="live", model_session_budget_cents=500, model_daily_budget_cents=5000,
                    model_allow_non_standard_demo=True)
    config = budget_config_from_settings(demo)
    assert (config.session_limit_cents, config.daily_limit_cents) == (500, 5000)
    assert config.allow_non_standard_demo is True and config.verified_standard_access is False
    request = ReservationRequest(uuid4(), uuid4(), uuid4(), uuid4(),
                                 datetime.now(timezone.utc),
                                 "sam-3.1", "meta-docs-2026-10-28", UsageUnits(images=1))
    assert BudgetService(None, config)._configured_cost(request) == 2500  # type: ignore[arg-type]
    blocked = BudgetConfig(model_mode="live")
    with pytest.raises(BudgetError, match="standard_access_unverified"):
        BudgetService(None, blocked)._configured_cost(request)  # type: ignore[arg-type]


def test_worker_live_gates_name_missing_inputs_without_secrets() -> None:
    assert "MODEL_API_KEY is required" in (live_configuration_error(Settings(model_mode="live")) or "")
    ready = Settings(model_mode="live", model_api_key=SecretStr("test-key-for-tests"), model_allow_non_standard_demo=True)
    assert live_configuration_error(ready) is None
    bad_price = Settings(model_mode="live", model_api_key=SecretStr("test-key-for-tests"), model_allow_non_standard_demo=True,
                         model_price_version="unknown")
    assert "MODEL_PRICE_VERSION is unverified" in (live_configuration_error(bad_price) or "")


def test_live_worker_registry_builds_real_segmenter() -> None:
    from app.adapters.meta.transport import MetaSamSegmenter
    from app.jobs.worker import build_registry

    settings = Settings(model_mode="live", model_api_key=SecretStr("test-key-for-tests"),
                        model_allow_non_standard_demo=True)
    registry = build_registry(settings, object())
    assert "segment" in registry.stages and "source_ingest" in registry.stages
    built = registry.handler("segment").segmenter_factory(1)  # type: ignore[attr-defined]
    assert isinstance(built, MetaSamSegmenter)


def test_pending_verifier_verdict_shape_is_valid() -> None:
    from app.domain.verification import parse_verdict

    payload = {"finding_id": str(uuid4()), "verdict": "unverifiable", "concept": "rust",
               "severity": None, "confidence": None,
               "evidence_note": "Live verification unavailable in demo stage 1; candidate preserved unverified.",
               "subzone": None, "component_id": None, "duplicate_of": None, "flags": []}
    verdict = parse_verdict(payload)
    assert verdict.verdict.value == "unverifiable" and verdict.severity is None


def test_pending_verifier_marks_live_candidate_unverified() -> None:
    from app.contracts.enums import Concept, FindingStatus
    from app.contracts.evidence import BoundingBox, EvidenceRef, Finding
    from app.domain.verification import parse_verdict, resolve_verdict
    from app.pipeline.orchestration import load_pipeline_config

    candidate = Finding(id=uuid4(), session_id=uuid4(), run_id=uuid4(), part_id=uuid4(), concept_run_id=uuid4(),
                        part_no=1, zone="driver_side", track_id="t1", status=FindingStatus.CANDIDATE,
                        concept=Concept.RUST,
                        evidence=EvidenceRef(frame_key="media/test/f0.jpg", frame_index=0, frame_width=100,
                                             frame_height=100, bbox=BoundingBox(x=0.25, y=0.25, w=0.5, h=0.5)),
                        evidence_note="live candidate")
    raw = LivePendingVerifier().verify_raw(candidate, (candidate,), "rubric")
    resolution = resolve_verdict(candidate, parse_verdict(raw), load_pipeline_config().rubrics)
    assert resolution.status == FindingStatus.UNVERIFIED and resolution.severity is None
