"""ElevenLabs text-to-speech adapter.

Verified against https://elevenlabs.io/docs/api-reference/text-to-speech/convert :
POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128,
header `xi-api-key`, JSON body {"text", "model_id"}. Voice and model IDs come from the
environment and are required; none is invented here.

Each request is prepared first (exact body bytes, SHA-256, character cap of 2,500 UTF-16 units),
sent exactly once and only behind a dispatch grant. No retries. The response is billed by
characters, but this adapter reads no billing headers (their names are unverified), so success
reports the request's character count only; the ledger must keep the charge reserved until
verified billing evidence exists. The key and the text are never logged or placed in errors.
"""

import json

import httpx

from app.briefing.dispatch import (DispatchGrant, PreparedRequest, PreparedUnits, Usage, UsageRecorder,
                                   execute_once, prepare_request, utf16_units)

from . import VoiceAudio, VoiceProviderError
from .config import BASE_URL, OUTPUT_FORMAT, TIMEOUT_SECONDS, VoiceEnv

PROVIDER = "elevenlabs"


class ElevenLabsVoice:
    def __init__(self, env: VoiceEnv, transport: httpx.BaseTransport | None = None) -> None:
        self._env = env
        self._transport = transport

    def _url(self) -> str:
        return f"{BASE_URL}/v1/text-to-speech/{self._env.voice_id}?output_format={OUTPUT_FORMAT}"

    def prepare(self, text: str, kind: str = "narration") -> PreparedRequest:
        body = json.dumps({"text": text, "model_id": self._env.model_id}).encode()
        return prepare_request(kind=kind, provider=PROVIDER, model=self._env.model_id, url=self._url(), body=body,
                               units=PreparedUnits(characters=utf16_units(text)), voice_id=self._env.voice_id)

    def synthesize(self, text: str, *, grant: DispatchGrant | None = None, record: UsageRecorder | None = None,
                   kind: str = "narration", deadline_seconds: float | None = None) -> VoiceAudio:
        return self.synthesize_prepared(self.prepare(text, kind), grant=grant, record=record,
                                        deadline_seconds=deadline_seconds)

    def synthesize_prepared(self, prepared: PreparedRequest, *, grant: DispatchGrant | None,
                            record: UsageRecorder | None, deadline_seconds: float | None = None) -> VoiceAudio:
        if prepared.provider != PROVIDER or prepared.model != self._env.model_id \
                or prepared.voice_id != self._env.voice_id or prepared.url != self._url():
            raise VoiceProviderError("prepared request does not match this voice", code="prepared_request_invalid")
        timeout = TIMEOUT_SECONDS if deadline_seconds is None else min(TIMEOUT_SECONDS, deadline_seconds)
        if timeout <= 0.05:
            raise VoiceProviderError("voice deadline already reached; nothing was sent", code="deadline")

        def send() -> tuple[int, bytes]:
            try:
                with httpx.Client(transport=self._transport, timeout=timeout) as client:
                    response = client.post(prepared.url, content=prepared.body,
                                           headers={"xi-api-key": self._env.api_key,
                                                    "Content-Type": "application/json"})
            except httpx.HTTPError as exc:
                code = "timeout" if isinstance(exc, httpx.TimeoutException) else "transport"
                raise VoiceProviderError(f"voice provider request failed ({type(exc).__name__})",
                                         code=code) from None
            return response.status_code, response.content

        def usage_of(status: int, raw: bytes) -> Usage | None:
            return Usage(characters=prepared.units.characters) if status == 200 else None

        def interpret(status: int, raw: bytes) -> tuple[VoiceAudio, bool]:
            if status != 200:
                raise VoiceProviderError(f"voice provider returned HTTP {status}", code=f"http_{status}",
                                         http_status=status)
            if not raw:
                raise VoiceProviderError("voice provider returned empty audio", code="empty_audio")
            return VoiceAudio(data=raw, content_type="audio/mpeg", extension="mp3", provenance="live"), True

        return execute_once(prepared, grant, record, send=send, usage_of=usage_of, interpret=interpret,
                            fail_label="voice provider", error_type=VoiceProviderError)
