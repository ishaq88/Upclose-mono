"""Content-addressed audio cache over ObjectStorage, shared by narration and the voice guide."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from app.adapters.storage import ObjectStorage, StorageError

MAX_AUDIO_BYTES = 8 * 1024 * 1024

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def input_hash(text: str, voice_id: str, model_id: str, provider: str) -> str:
    """sha256 over (text, voice, model, provider); JSON-encoded so fields cannot run together."""
    payload = json.dumps([text, voice_id, model_id, provider], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def fetch_or_store(storage: ObjectStorage, key: str, content_type: str,
                   synthesize: Callable[[], bytes], *, strict: bool = False) -> tuple[bytes, bool]:
    """Return (audio, cache_hit). A hit never calls `synthesize`. Storage trouble on read
    falls through to synthesis, unless `strict` (paid providers): then the StorageError propagates,
    so an unreadable cache can never trigger a paid call. A failed write still returns the audio."""
    with _lock_for(key):
        with tempfile.TemporaryDirectory(prefix="upclose-audio-") as scratch:
            path = Path(scratch) / "audio"
            try:
                if storage.head_size(key) is not None:
                    storage.download_to(key, path, MAX_AUDIO_BYTES)
                    data = path.read_bytes()
                    if data:
                        return data, True
            except (StorageError, OSError):
                if strict:
                    raise StorageError("audio cache is unavailable") from None
            data = synthesize()
            try:
                path.write_bytes(data)
                storage.put_file(key, path, content_type)
            except (StorageError, OSError):
                pass
            return data, False
