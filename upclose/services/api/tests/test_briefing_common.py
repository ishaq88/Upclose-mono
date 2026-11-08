"""Shared helpers for briefing tests: in-memory storage, synthetic summaries and report."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from app.adapters.storage import DownloadedObject, StorageError
from app.adapters.voice import VoiceAudio
from app.contracts.reports import PaidReport
from tests.test_advisory_adapters import summaries  # noqa: F401  (synthetic rig + OBD summaries)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION = UUID("00000000-0000-4000-8000-0000000000aa")
KEY = "sk_test_SECRET123"
ELEVEN_ENV = {"VOICE_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": KEY,
              "ELEVENLABS_VOICE_ID": "voiceX", "ELEVENLABS_MODEL_ID": "modelY"}


class MemoryStorage:
    """Just the ObjectStorage methods the audio cache uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def head_size(self, key: str) -> int | None:
        return len(self.objects[key]) if key in self.objects else None

    def download_to(self, key: str, path: Path, max_bytes: int) -> DownloadedObject:
        if key not in self.objects:
            raise StorageError("NoSuchKey")
        path.write_bytes(self.objects[key])
        return DownloadedObject(size_bytes=len(self.objects[key]), sha256="0" * 64)

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        self.puts += 1
        self.objects[key] = path.read_bytes()


def allow(_prepared) -> bool:
    return True


class CountingVoice:
    def __init__(self, data: bytes = b"AUDIO", extension: str = "wav", fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._data, self._ext, self._fail = data, extension, fail

    def synthesize(self, text: str, *, grant=None, record=None, kind="narration",
                   deadline_seconds=None) -> VoiceAudio:
        self.calls.append(text)
        if self._fail:
            raise self._fail
        return VoiceAudio(data=self._data, content_type="audio/wav", extension=self._ext, provenance="synthetic")


def paid_report(narration: list[str] | None = None, **overrides) -> PaidReport:
    data = json.loads((FIXTURES / "paid_report.json").read_text())["data"]
    texts = narration if narration is not None else ["The car has two flagged findings."]
    data["narration"] = [{"text": text, "finding_ids": []} for text in texts]
    data.update(overrides)
    return PaidReport.model_validate(data)
