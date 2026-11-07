"""ElevenLabs text-to-speech adapter.

Verified against https://elevenlabs.io/docs/api-reference/text-to-speech/convert :
POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128,
header `xi-api-key`, JSON body {"text", "model_id"}. Voice and model IDs come from the
environment and are required; none is invented here. One call, no retries. The key and
the text are never logged or placed in error messages.
"""

import httpx

from . import VoiceAudio, VoiceProviderError
from .config import BASE_URL, OUTPUT_FORMAT, TIMEOUT_SECONDS, VoiceEnv


class ElevenLabsVoice:
    def __init__(self, env: VoiceEnv, transport: httpx.BaseTransport | None = None) -> None:
        self._env = env
        self._transport = transport

    def synthesize(self, text: str) -> VoiceAudio:
        url = f"{BASE_URL}/v1/text-to-speech/{self._env.voice_id}"
        try:
            with httpx.Client(transport=self._transport, timeout=TIMEOUT_SECONDS) as client:
                response = client.post(url, params={"output_format": OUTPUT_FORMAT},
                                       headers={"xi-api-key": self._env.api_key, "Content-Type": "application/json"},
                                       json={"text": text, "model_id": self._env.model_id})
        except httpx.HTTPError as exc:
            raise VoiceProviderError(f"voice provider request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise VoiceProviderError(f"voice provider returned HTTP {response.status_code}")
        if not response.content:
            raise VoiceProviderError("voice provider returned empty audio")
        return VoiceAudio(data=response.content, content_type="audio/mpeg", extension="mp3", provenance="live")
