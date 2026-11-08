"""Spark (`muse-spark-1.3`) dash reader over the Responses API, same conventions as the verifier.

One user message: input_text instructions plus one input_image (the cluster crop as JPEG).
Strict JSON schema output, `store: false`, large `max_output_tokens` because Spark reasons
before answering. Budget: the caller supplies `grant(input_token_bound)` which must reserve
before the request is sent; settlement from the reported usage is the caller's job. The
request body holds image bytes and is never logged; errors carry stable codes only.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable

from PIL import Image, UnidentifiedImageError

from app.adapters.meta.parser import ProviderFailure
from app.domain.dash_lights import reading_schema
from . import PROMPT, Grant, ReaderOutput, accept

MODEL = "muse-spark-1.3"
MAX_OUTPUT_TOKENS = 8192
MAX_PIXELS = 3840 * 2160
MAX_SIDE = 1600
MAX_RESPONSE_BYTES = 256 * 1024
IMAGE_TOKEN_BLOCK_PIXELS = 8 * 8
TIMEOUT_SECONDS = 120.0
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def _transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https base checked
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ProviderFailure("provider_timeout") from None


def _same_model(model: object) -> bool:
    return model == MODEL or (isinstance(model, str) and model.startswith(MODEL + "-")
                              and _SAFE_TOKEN.fullmatch(model) is not None)


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class MetaDashReader:
    requires_budget = True

    def __init__(self, *, api_key: str, api_base: str = "https://api.meta.ai",
                 transport: Transport = _transport) -> None:
        if not api_key or not api_base.startswith("https://"):
            raise ProviderFailure("standard_access_unverified")
        self._key, self._base, self._transport = api_key, api_base.rstrip("/"), transport

    def prepare(self, image: bytes) -> tuple[bytes, int]:
        """Returns (request body, conservative input-token upper bound)."""
        try:
            with Image.open(io.BytesIO(image)) as picture:
                if picture.width * picture.height > MAX_PIXELS or getattr(picture, "n_frames", 1) != 1:
                    raise ProviderFailure("malformed_record")
                picture.load()
                rgb = picture.convert("RGB")
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise ProviderFailure("malformed_record") from None
        if max(rgb.size) > MAX_SIDE:
            scale = MAX_SIDE / max(rgb.size)
            rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))))
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=90)
        url = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        body = json.dumps({"model": MODEL, "store": False, "max_output_tokens": MAX_OUTPUT_TOKENS,
                           "input": [{"role": "user", "content": [
                               {"type": "input_text", "text": PROMPT},
                               {"type": "input_image", "image_url": url}]}],
                           "text": {"format": {"type": "json_schema", "name": "dash_reading",
                                               "strict": True, "schema": reading_schema()}}},
                          separators=(",", ":")).encode()
        bound = len(PROMPT.encode()) + math.ceil(rgb.width * rgb.height / IMAGE_TOKEN_BLOCK_PIXELS)
        return body, bound

    def read(self, image: bytes, *, grant: Grant | None = None) -> ReaderOutput:
        body, bound = self.prepare(image)
        if grant is None:
            raise ProviderFailure("budget_not_reserved")
        try:
            authorized = grant(bound)
        except Exception:
            raise ProviderFailure("budget_not_reserved") from None
        if authorized is not True:
            raise ProviderFailure("budget_not_reserved")
        try:
            status, raw = self._transport(f"{self._base}/v1/responses", body,
                                          {"Authorization": "Bearer " + self._key,
                                           "Content-Type": "application/json"}, TIMEOUT_SECONDS)
        except ProviderFailure:
            raise
        except Exception:
            raise ProviderFailure("provider_timeout") from None
        if status in (401, 403):
            raise ProviderFailure("standard_access_unverified", http_status=status)
        if status == 429 or status >= 500:
            raise ProviderFailure("provider_timeout", http_status=status)
        if status != 200:
            raise ProviderFailure("provider_failed", http_status=status)
        if len(raw) > MAX_RESPONSE_BYTES:
            return ReaderOutput("meta", MODEL, None, "invalid_response:too_large")
        try:
            payload = json.loads(raw)
        except ValueError:
            return ReaderOutput("meta", MODEL, None, "invalid_response:envelope")
        if not isinstance(payload, dict) or not _same_model(payload.get("model")):
            return ReaderOutput("meta", MODEL, None, "invalid_response:envelope")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        inputs, outputs = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
        if payload.get("status") != "completed":
            # Reasoning can exhaust the output budget: billed, incomplete, never re-sent here.
            return ReaderOutput("meta", MODEL, None, "invalid_response:incomplete", inputs, outputs)
        text = _message_text(payload)
        if text is None:
            return ReaderOutput("meta", MODEL, None, "invalid_response:no_text", inputs, outputs)
        return accept(text, provider="meta", model=MODEL, provenance="live",
                      input_tokens=inputs, output_tokens=outputs)


def _message_text(payload: dict) -> str | None:
    items = payload.get("output")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                return part["text"]
    return None
