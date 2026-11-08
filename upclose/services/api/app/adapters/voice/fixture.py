"""Fixture voice provider: a short silent WAV, labeled synthetic. Never opens a socket."""

import io
import wave

from . import VoiceAudio


def silent_wav(seconds: float = 0.5, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


class FixtureVoice:
    def synthesize(self, text: str, *, grant=None, record=None, kind="narration",
                   deadline_seconds=None) -> VoiceAudio:
        return VoiceAudio(data=silent_wav(), content_type="audio/wav", extension="wav", provenance="synthetic")
