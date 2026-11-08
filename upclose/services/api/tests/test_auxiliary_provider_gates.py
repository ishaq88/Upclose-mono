"""Unfunded auxiliary generation must never invoke a live transport. Synthetic fixtures remain available."""
from types import SimpleNamespace

import pytest

from app.adapters.voice import VoiceProviderError
from app.pipeline.briefing import build_briefing
from app.pipeline.report_stages import _narration_audio_for
from app.services.auxiliary_providers import guarded_guide_adapter
from app.settings import Settings


@pytest.mark.parametrize("provider", ["meta", "gemini"])
def test_live_advisory_is_explicitly_unavailable_without_invocation(monkeypatch, provider):
    def forbidden(*args, **kwargs):
        pytest.fail("live advisory dispatch must require a priced reservation")

    monkeypatch.setattr("app.pipeline.briefing.build_advisory_block", forbidden)
    result = build_briefing((), None, "synthetic-session", Settings(), environ={"ADVISOR_PROVIDER": provider})
    assert result.advisory.status == "not_available"
    assert "priced budget reservation" in result.advisory.reason
    assert result.advisory.provider == provider


def test_live_narration_is_not_invoked_without_reservation(monkeypatch):
    monkeypatch.setenv("VOICE_PROVIDER", "elevenlabs")
    monkeypatch.setattr("app.briefing.narration_audio.build_narration_audio",
        lambda *args, **kwargs: pytest.fail("unfunded synthesis was dispatched"))
    snapshot = SimpleNamespace(paid_narration=SimpleNamespace(briefing=SimpleNamespace(narration_audio=None)))
    assert _narration_audio_for(snapshot, object()) is None


def test_voice_guide_live_synthesis_denied_but_fixture_not_replaced():
    assert guarded_guide_adapter({"VOICE_PROVIDER": "fixture"}) is None
    denied = guarded_guide_adapter({"VOICE_PROVIDER": "elevenlabs"})
    with pytest.raises(VoiceProviderError, match="budget reservation unavailable"):
        denied.synthesize("synthetic caption")
