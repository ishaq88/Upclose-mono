"""Advisory and narration callsites: lease, first persisted result, never repeat an unknown call.

Fake money on an isolated PostgreSQL database; the provider-facing function is a stand-in that runs the real
dispatch seam (`execute_once`), so grant, record and verification behave exactly as the adapters drive them.
"""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from test_auxiliary_budget import db, factory, live, make_lease, rows  # noqa: F401  (shared isolated-database fixtures)

from app.adapters.storage import StorageError
from app.briefing.advisory_block import AdvisoryBlock, BlockItem
from app.briefing.narration_audio import NarrationAudioError
from app.briefing.dispatch import DispatchError, PreparedUnits, Usage, execute_once, prepare_request
from app.pipeline.briefing import build_briefing
from app.pipeline.report_stages import _narration_audio_for
from app.services.auxiliary_budget import AuxiliaryDispatcher
from app.services.budgets import BudgetService
from app.settings import Settings


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def head_size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def download_to(self, key, path, max_bytes):
        if key not in self.objects:
            raise StorageError("missing")
        Path(path).write_bytes(self.objects[key])

    def put_file(self, key, path, content_type):
        self.objects[key] = Path(path).read_bytes()


def available_block():
    return AdvisoryBlock(status="available", provider="meta", model="muse-spark-1.3", prompt_version="advisory-v1",
                         provenance="live", summary="Synthetic summary.",
                         questions_for_seller=(BlockItem(text="Ask about the service history.", cites=("obd",)),))


def unavailable_block(error):
    return AdvisoryBlock(status="not_available", provider="meta", model="unknown", prompt_version="advisory-v1",
                         provenance="live", reason=str(error)[:200])


def advisory_request():
    body = json.dumps({"model": "muse-spark-1.3", "input": "synthetic prompt", "max_output_tokens": 2048}).encode()
    return prepare_request(kind="advisory", provider="meta", model="muse-spark-1.3", url="https://example.invalid/v1/responses",
                           body=body, units=PreparedUnits(input_tokens=len(body), output_tokens=2048))


def stub_builder(send, usage_of):
    def build(summaries, vin_info, settings, *, environ=None, grant=None, record=None, deadline_seconds=45.0, **_):
        try:
            return execute_once(advisory_request(), grant, record, send=send, usage_of=usage_of,
                                interpret=lambda status, raw: (available_block(), True), fail_label="advisory")
        except DispatchError as error:
            return unavailable_block(error)
    return build


ENV = {"ADVISOR_PROVIDER": "meta"}


def test_live_advisory_is_reserved_dispatched_settled_stored_and_never_asked_twice(db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.pipeline.briefing.build_advisory_block", stub_builder(
        lambda: (sent.append(1) or (200, b"{}")),
        lambda status, raw: Usage(input_tokens=500, cached_input_tokens=0, output_tokens=100, provider_request_id="resp-synthetic")))
    storage, lease = MemoryStorage(), make_lease(db)
    first = build_briefing((), None, str(lease.session_id), Settings(), environ=ENV,
                           dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), lease), storage=storage)
    assert first.advisory.status == "available" and sent == [1]
    calls, buckets = rows(db)
    assert len(calls) == 1 and calls[0].state == "succeeded" and calls[0].actual_cost == 1050  # 500*1.25 + 100*4.25 micro-USD
    assert calls[0].model == "muse-spark-1.3" and len(storage.objects) == 1
    assert [b.reserved_cents for b in buckets] == [0, 0] and [b.spent_cents for b in buckets] == [1, 1]
    # A stage retry under a new lease serves the first persisted answer: no grant, no transport.
    retry = make_lease(db, lease.session_id)
    again = build_briefing((), None, str(lease.session_id), Settings(), environ=ENV,
                           dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), retry), storage=storage)
    assert again.advisory == first.advisory and sent == [1] and len(rows(db)[0]) == 1
    # Even with the stored answer gone, the ledger row blocks a second request: unavailable, not a second charge.
    lost = build_briefing((), None, str(lease.session_id), Settings(), environ=ENV,
                          dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), make_lease(db, lease.session_id)),
                          storage=MemoryStorage())
    assert lost.advisory.status == "not_available" and sent == [1] and len(rows(db)[0]) == 1


def test_live_advisory_timeout_after_the_grant_keeps_an_unknown_charge_and_is_stored(db, monkeypatch):
    def send():
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr("app.pipeline.briefing.build_advisory_block", stub_builder(send, lambda status, raw: None))
    storage, lease = MemoryStorage(), make_lease(db)
    result = build_briefing((), None, str(lease.session_id), Settings(), environ=ENV,
                            dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), lease), storage=storage)
    assert result.advisory.status == "not_available" and len(storage.objects) == 1  # the unavailable answer is the first result
    calls, buckets = rows(db)
    assert calls[0].state == "unknown" and calls[0].actual_cost is None
    assert calls[0].requested_units["failure"]["code"] == "provider_failed"  # sanitized: the exception name never reaches the ledger
    assert all(b.reserved_cents > 0 and b.spent_cents == 0 for b in buckets)


def test_live_advisory_denied_by_a_lost_lease_never_reaches_transport(db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.pipeline.briefing.build_advisory_block",
                        stub_builder(lambda: (sent.append(1) or (200, b"{}")), lambda status, raw: None))
    lease, storage = make_lease(db), MemoryStorage()
    result = build_briefing((), None, str(lease.session_id), Settings(), environ=ENV,
                            dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), replace(lease, token=uuid4())),
                            storage=storage)
    assert result.advisory.status == "not_available" and sent == [] and rows(db) == ([], []) and storage.objects == {}


def narration_snapshot():
    return SimpleNamespace(session_id=uuid4(), manifest=SimpleNamespace(provenance="live"),
                           paid_narration=SimpleNamespace(briefing=SimpleNamespace(narration_audio=None)))


def test_live_narration_is_reserved_at_the_rate_dispatched_and_left_unknown(db, monkeypatch):
    import app.pipeline.report_stages as stages
    sent = []

    def fake_audio(paid, session_id, storage, *, simulated=False, grant=None, record=None, deadline_seconds=None):
        body = json.dumps({"text": "x" * 1000, "model_id": "eleven_v4"}).encode()
        prepared = prepare_request(kind="narration", provider="elevenlabs", model="eleven_v4", url="https://example.invalid/tts",
                                   body=body, units=PreparedUnits(characters=1000), voice_id="synthetic-voice")
        try:
            execute_once(prepared, grant, record, send=lambda: (sent.append(1) or (200, b"audio")),
                         usage_of=lambda status, raw: Usage(characters=1000), interpret=lambda status, raw: (None, True),
                         fail_label="narration")
        except DispatchError:
            raise NarrationAudioError("voice provider failed") from None
        return SimpleNamespace(metadata=lambda: {"provider": "elevenlabs", "voice_id": "synthetic-voice", "model_id": "eleven_v4",
                                                 "provenance": "live", "sha256": "a" * 64, "storage_key": "media/x/narration/y.mp3",
                                                 "content_type": "audio/mpeg", "size_bytes": 5, "label": "synthesized voice"})

    monkeypatch.setenv("VOICE_PROVIDER", "elevenlabs")
    monkeypatch.setattr("app.briefing.narration_audio.build_narration_audio", fake_audio)
    monkeypatch.setattr(stages, "replace", lambda snapshot, **_: snapshot)
    monkeypatch.setattr(stages, "build_reports", lambda snapshot: (None, "paid"))
    monkeypatch.setattr(stages, "PaidReport", str)
    lease = make_lease(db)
    meta = _narration_audio_for(narration_snapshot(), MemoryStorage(),
                                dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), lease))
    assert meta is not None and meta.provenance == "live" and sent == [1]
    calls, buckets = rows(db)
    assert calls[0].model == "elevenlabs/eleven_v4" and calls[0].reserved_cost == 80000 and calls[0].state == "unknown"
    assert calls[0].requested_units["failure"]["code"] == "billing_unverified" and calls[0].actual_cost is None
    assert [b.reserved_cents for b in buckets] == [8, 8]
    # The same render retried after that attempt is not asked again; the report publishes without audio.
    retried = _narration_audio_for(narration_snapshot(), MemoryStorage(),
                                   dispatcher=AuxiliaryDispatcher(BudgetService(db, live()), make_lease(db, lease.session_id)))
    assert retried is None and sent == [1] and len(rows(db)[0]) == 1
