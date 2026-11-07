"""Live SAM transport (demo track). Doc-derived Responses API; pinned after first bounded smoke.

Request shape follows the T04a doc snapshot in `.docs/evidence/provider-contract.md`:
`POST {META_API_BASE}/v1/responses`, model `sam-3.1`, one user message with `input_text`
plus `input_image`/`input_video`. Stage 1 inlines base64 data URLs (no Files API); media
above `MAX_INLINE_BYTES` fails closed as unsupported, never truncated. Responses must be
`completed` with special-token `output_text`; usage comes from `usage.video_frames_processed`
/ `images_processed` and stays unknown (None) when the provider omits it. No payloads, keys
or media references are logged. Transport returns `live` provenance; Standard vs non-Standard
demo is decided by budget configuration, and the run is recorded under live provider
identities so reports never read as synthetic.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from app.adapters.storage import ObjectStorage, StorageError
from .parser import ProviderFailure, normalize_output
from .types import MediaRef, ProviderUsage, SegmentResult

MAX_INLINE_BYTES = 24 * 1024 * 1024
TIMEOUT_SECONDS = 120.0
MODEL_ID = "sam-3.1"
PRICE_VERSION = "meta-docs-2026-10-28"

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https base
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderFailure("provider_timeout") from None


def _output_text(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                return part["text"]
    return None


def _usage(payload: object) -> ProviderUsage:
    raw = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return ProviderUsage()

    def count(key: str) -> int | None:
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    return ProviderUsage(images_processed=count("images_processed"),
                         video_frames_processed=count("video_frames_processed"))


class MetaSamSegmenter:
    """Live Segmenter over the documented Responses API. `requires_budget` tells the
    segment handler to reserve/dispatch/settle every request before sending."""

    requires_budget = True
    model = MODEL_ID
    price_version = PRICE_VERSION

    def __init__(self, *, api_key: str, api_base: str, storage: ObjectStorage,
                 prompt_texts: dict[str, str], transport: Transport = urllib_transport) -> None:
        if not api_key or not api_base.startswith("https://"):
            raise ProviderFailure("standard_access_unverified")
        self._api_key, self._api_base = api_key, api_base.rstrip("/")
        self._storage, self._texts, self._transport = storage, dict(prompt_texts), transport

    def segment(self, media_ref: MediaRef, prompt_id: str, request_id: UUID) -> SegmentResult:
        text = self._texts.get(prompt_id)
        if not text:
            raise ProviderFailure("model_unverified")
        media_part, _submitted = self._media_part(media_ref)
        body = json.dumps({"model": MODEL_ID, "input": [{"role": "user", "content": [
            {"type": "input_text", "text": text}, media_part]}]}).encode()
        status, raw = self._transport(f"{self._api_base}/v1/responses", body,
                                      {"Authorization": "Bearer " + self._api_key,
                                       "Content-Type": "application/json"}, TIMEOUT_SECONDS)
        if status in (401, 403):
            raise ProviderFailure("standard_access_unverified", http_status=status)
        if status == 429 or status >= 500:
            raise ProviderFailure("provider_timeout", http_status=status)
        if status != 200:
            raise ProviderFailure("provider_failed", http_status=status)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ProviderFailure("malformed_record") from None
        if isinstance(payload, dict) and payload.get("model") not in (None, MODEL_ID):
            raise ProviderFailure("model_unverified")
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ProviderFailure("incomplete_response")
        output = _output_text(payload)
        if output is None:
            raise ProviderFailure("malformed_record")
        return normalize_output(output, media_ref, prompt_id, request_id,
                                usage=_usage(payload), provenance="live")

    def _media_part(self, media_ref: MediaRef) -> tuple[dict[str, str], int]:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "media.bin"
            try:
                downloaded = self._storage.download_to(media_ref.media_key, path, MAX_INLINE_BYTES)
            except StorageError:
                raise ProviderFailure("provider_failed") from None
            if downloaded.size_bytes > MAX_INLINE_BYTES:
                raise ProviderFailure("model_unverified")
            data = path.read_bytes()
            if media_ref.content_sha256 is not None and hashlib.sha256(data).hexdigest() != media_ref.content_sha256:
                raise ProviderFailure("malformed_record")
            encoded = base64.b64encode(data).decode()
        if media_ref.kind == "video":
            return {"type": "input_video", "video_url": f"data:video/mp4;base64,{encoded}"}, len(media_ref.frames)
        mime = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
        return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}, 1
