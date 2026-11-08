"""Engine-audio source: private upload, verification, worker derivation, gated report media, purge.

Runs against real PostgreSQL and the Floci object-store emulator (no skips when the S3_* endpoints are set).
"""
from __future__ import annotations

import hashlib
import io
import math
import shutil
import struct
import subprocess
import wave
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.contracts.enums import SessionState
from app.jobs.queue import JobQueue
from app.models import AudioUpload, EvidenceSource, Report, Session
from app.pipeline.audio_ingest import register_audio_handlers
from app.pipeline.purge import PurgeHandler
from app.report import render_report
from app.services.deletion import request_deletion
from app.services.snapshots import build_free_report, build_paid_report
from tests.test_ingest import accept, clip, media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import Pipe, finalize, isolate, rows, setup  # noqa: F401
from tests.test_report_access import grant
from tests.test_report_stages import RUST, Rig
from tests.test_sources_domain import envelope
from tests.test_uploads import bucket, make, session_factory  # noqa: F401

MIB = 1024 * 1024
needs_media = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                 reason="ffmpeg/ffprobe not installed")


def wav_bytes(seconds: float = 3.0, freq: float = 110.0, rate: int = 8000, amp: float = 0.5) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * freq * i / rate)))
                                 for i in range(int(seconds * rate))))
    return out.getvalue()


def encoded(fmt_args: list[str], suffix: str, tmp_path) -> bytes:
    target = tmp_path / f"clip{suffix}"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=140:duration=3",
                          *fmt_args, str(target)], capture_output=True)
    if run.returncode != 0:
        pytest.skip("ffmpeg lacks the encoder: " + run.stderr.decode()[:80])
    return target.read_bytes()


def upload(h, data: bytes, content_type: str = "audio/wav", *, declared: int | None = None):
    created = h.http.post(f"/api/v1/sessions/{h.session_id}/audio", headers=h.headers,
                          json={"content_type": content_type, "content_length": declared or len(data)})
    if created.status_code != 201:
        return created, None
    upload_id = created.json()["upload_id"]
    chunk = 8 * MIB
    pieces = [data[i:i + chunk] for i in range(0, len(data), chunk)]
    urls = h.http.post(f"/api/v1/audio/{upload_id}/urls", headers=h.headers,
                       json={"part_numbers": list(range(1, len(pieces) + 1))})
    assert urls.status_code == 200, urls.text
    for item, piece in zip(urls.json()["items"], pieces):
        assert httpx.put(item["url"], content=piece, headers=item["headers"], timeout=60).status_code == 200
    return h.http.post(f"/api/v1/audio/{upload_id}/complete", headers=h.headers, json={}), upload_id


def attach(h, upload_id, sha: str, provenance: str = "synthetic"):
    return h.http.post(f"/api/v1/sessions/{h.session_id}/sources", headers=h.headers, json=envelope(
        "engine_audio", {"upload_id": upload_id, "sha256": sha}, provenance=provenance, device="mic-bench"))


def attach_clip(h, data: bytes, content_type: str = "audio/wav", provenance: str = "synthetic"):
    done, upload_id = upload(h, data, content_type)
    assert done.status_code == 200, done.text
    response = attach(h, upload_id, hashlib.sha256(data).hexdigest(), provenance)
    assert response.status_code == 201, response.text
    return upload_id, response


def pipe_for(h) -> Pipe:
    pipe = Pipe(h, script=RUST)
    register_audio_handlers(pipe.registry, h.db, h.storage, h.settings)
    return pipe


def summary_of(h) -> dict:
    (row,) = rows(h, EvidenceSource, EvidenceSource.kind == "engine_audio")
    return row.summary


# ---- upload limits ------------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"content_type": "audio/wav", "content_length": 10 * MIB + 1},
    {"content_type": "audio/wav", "content_length": 0},
    {"content_type": "video/mp4", "content_length": 1000},
    {"content_type": "audio/ogg", "content_length": 1000},
    {"content_type": "audio/wav", "content_length": 1000, "extra": 1},
])
def test_upload_limits_and_types_are_422(make, body):
    h = make()
    assert h.http.post(f"/api/v1/sessions/{h.session_id}/audio", headers=h.headers, json=body).status_code == 422
    assert rows(h, AudioUpload) == []


def test_ten_mib_is_accepted_for_creation_and_one_open_attempt_at_a_time(make):
    h = make()
    first = h.http.post(f"/api/v1/sessions/{h.session_id}/audio", headers=h.headers,
                        json={"content_type": "audio/mpeg", "content_length": 10 * MIB})
    assert first.status_code == 201 and first.json()["chunk_count"] == 2
    again = h.http.post(f"/api/v1/sessions/{h.session_id}/audio", headers=h.headers,
                        json={"content_type": "audio/wav", "content_length": 1000})
    assert again.status_code == 409
    assert h.http.delete(f"/api/v1/audio/{first.json()['upload_id']}", headers=h.headers).status_code == 204
    assert h.http.post(f"/api/v1/sessions/{h.session_id}/audio", headers=h.headers,
                       json={"content_type": "audio/wav", "content_length": 1000}).status_code == 201


def test_auth_ownership_and_state(make):
    h = make()
    path = f"/api/v1/sessions/{h.session_id}/audio"
    body = {"content_type": "audio/wav", "content_length": 1000}
    assert h.http.post(path, json=body).status_code == 401
    assert h.http.post(path, json=body, headers=h.other_owner()).status_code == 404
    created = h.http.post(path, json=body, headers=h.headers).json()
    assert h.http.post(f"/api/v1/audio/{created['upload_id']}/complete", json={}, headers=h.other_owner()).status_code == 404
    assert h.http.post(f"/api/v1/sessions/{h.session_id}/finalize", headers=h.headers, json={}).status_code == 409  # open upload
    assert h.http.delete(f"/api/v1/audio/{created['upload_id']}", headers=h.headers).status_code == 204
    assert finalize(h)
    assert h.http.post(path, json=body, headers=h.headers).status_code == 409


def test_short_upload_cannot_complete(make):
    h = make()
    data = wav_bytes(1.0)
    done, _ = upload(h, data[:-100], declared=len(data))  # fewer bytes than declared
    assert 400 <= done.status_code < 500 and done.status_code != 401, done.text
    assert rows(h, AudioUpload)[0].state.value == "open"


# ---- verification ---------------------------------------------------------------------------------------


def test_hash_mismatch_is_rejected_and_the_clip_is_discarded(make):
    h = make()
    data = wav_bytes(2.0)
    done, upload_id = upload(h, data)
    assert done.status_code == 200
    response = attach(h, upload_id, "0" * 64)
    assert response.status_code == 422
    (row,) = rows(h, AudioUpload)
    assert row.state.value == "aborted" and row.reason == "AUDIO_HASH_MISMATCH"
    assert h.storage.head_size(row.object_key) is None
    assert rows(h, EvidenceSource) == []
    # the corrected clip can be sent afterwards
    attach_clip(h, data)


def test_attach_requires_a_completed_upload_of_this_session(make):
    h = make()
    other = make()
    data = wav_bytes(2.0)
    done, upload_id = upload(other, data)
    assert attach(h, upload_id, hashlib.sha256(data).hexdigest()).status_code == 404  # someone else's upload
    created = h.http.post(f"/api/v1/sessions/{h.session_id}/audio", headers=h.headers,
                          json={"content_type": "audio/wav", "content_length": len(data)}).json()
    assert attach(h, created["upload_id"], hashlib.sha256(data).hexdigest()).status_code == 409  # not completed
    assert attach(h, str(uuid4()), "a" * 64).status_code == 404


# ---- worker derivation ------------------------------------------------------------------------------------


@needs_media
def test_finalize_waits_for_processing_then_summary_is_assessed(make, scratch_root):  # noqa: F811
    h = setup(make, scratch_root)
    upload_id, response = attach_clip(h, wav_bytes(3.0))
    assert response.json()["summary"]["state"] == "processing" and response.json()["kind"] == "engine_audio"
    blocked = h.http.post(f"/api/v1/sessions/{h.session_id}/finalize", headers=h.headers, json={})
    assert blocked.status_code == 409
    pipe_for(h).drain()
    summary = summary_of(h)
    assert summary["state"] == "assessed", summary["reason"]
    assert abs(summary["duration_s"] - 3.0) < 0.1
    assert summary["peak_db"] <= 0 and len(summary["envelope_db"]) == 6 and len(summary["spectrogram_sha256"]) == 64
    (upload,) = rows(h, AudioUpload)
    assert upload.processing_state == "ready" and h.storage.head_size(upload.spectrogram_key) > 100
    assert upload.spectrogram_key.startswith(f"media/{h.session_id}/")
    listed = h.http.get(f"/api/v1/sessions/{h.session_id}/sources", headers=h.headers).json()["sources"]
    assert listed[0]["kind"] == "engine_audio" and listed[0]["summary"]["state"] == "assessed"
    assert finalize(h)


@needs_media
@pytest.mark.parametrize("data,reason", [
    (b"this is not audio at all" * 50, "could not be decoded"),
    (wav_bytes(125.0, rate=4000), "longer than 120"),
    (wav_bytes(3.0, amp=0.0), "silent"),
    (wav_bytes(0.3), "too short"),
])
def test_undecodable_long_silent_or_short_clips_are_not_assessed(make, scratch_root, data, reason):  # noqa: F811
    h = setup(make, scratch_root)
    attach_clip(h, data)
    pipe_for(h).drain()
    summary = summary_of(h)
    assert summary["state"] == "not_assessed" and reason in summary["reason"] and summary["spectrogram_sha256"] is None
    assert finalize(h)  # a failed decode never blocks finalization, and it is never a pass
    row = rows(h, AudioUpload)[0]
    assert row.processing_state == "failed" and row.spectrogram_key is None


@needs_media
@pytest.mark.parametrize("args,suffix,mime", [(["-c:a", "libmp3lame"], ".mp3", "audio/mpeg"),
                                              (["-c:a", "aac"], ".m4a", "audio/mp4")])
def test_mp3_and_m4a_clips_are_decoded(make, scratch_root, tmp_path, args, suffix, mime):  # noqa: F811
    h = setup(make, scratch_root)
    attach_clip(h, encoded(args, suffix, tmp_path), mime)
    pipe_for(h).drain()
    assert summary_of(h)["state"] == "assessed"


@needs_media
def test_replacing_the_clip_supersedes_the_old_job(make, scratch_root):  # noqa: F811
    h = setup(make, scratch_root)
    first_id, _ = attach_clip(h, wav_bytes(2.0))
    second_id, response = attach_clip(h, wav_bytes(4.0, freq=200.0))
    assert response.status_code == 201
    pipe_for(h).drain()
    summary = summary_of(h)
    assert summary["state"] == "assessed" and abs(summary["duration_s"] - 4.0) < 0.1
    states = {str(row.id): row.state.value for row in rows(h, AudioUpload)}
    assert states[first_id] == "aborted" and states[second_id] == "accepted"


# ---- report: entitlement-gated media, nothing in the free view ----------------------------------------------


@pytest.fixture
def audio_rig(make, media_dir, scratch_root):  # noqa: F811
    h = setup(make, scratch_root)
    attach_clip(h, wav_bytes(3.0))
    pipe = pipe_for(h)
    pipe.drain()
    accept(h, clip(media_dir), part_no=1)
    finalize(h)
    pipe.drain()
    rig = Rig(h, pipe)
    rig.install()
    pipe.drain()
    return rig


@needs_media
def test_free_view_gets_no_url_audio_image_or_route(audio_rig):
    rig = audio_rig
    slug = rows(rig.h, Report)[0].share_slug
    free = build_free_report(rig.snapshot())
    html, dumped = render_report(free), free.model_dump_json()
    for leak in ("<audio", "spectrogram", "/sources/engine_audio", "mic-bench", "envelope", "rms_db", "upload_id"):
        assert leak not in html and leak not in dumped, leak
    api = rig.h.http.get(f"/api/v1/reports/{slug}", headers=rig.h.headers)
    page = rig.h.http.get(f"/r/{slug}", headers=rig.h.headers)
    assert api.json()["scope"] == "free" and "<audio" not in page.text and "spectrogram" not in page.text
    assert "engine_audio/" not in page.text and "envelope" not in api.text
    for which in ("audio", "spectrogram"):
        path = f"/api/v1/reports/{slug}/sources/engine_audio/{which}"
        assert rig.h.http.get(path, headers=rig.h.headers).status_code == 404  # owner, but no entitlement
        assert rig.h.http.get(path).status_code == 404  # anonymous
        assert rig.h.http.get(path, headers=rig.h.other_owner()).status_code == 404
    assert {row.kind: row.checked for row in free.additional_checks}["engine_audio"] is True


@needs_media
def test_paid_view_serves_audio_and_spectrogram_through_entitlement(audio_rig):
    rig = audio_rig
    h = rig.h
    slug = rows(h, Report)[0].share_slug
    grant(rig)
    page = h.http.get(f"/r/{slug}", headers=h.headers)
    assert page.status_code == 200
    for expected in (f'<audio controls preload="none" src="/api/v1/reports/{slug}/sources/engine_audio/audio">',
                     f'src="/api/v1/reports/{slug}/sources/engine_audio/spectrogram"', "Simulated bench data",
                     "bench or internet sample", "Engine sound (audio clip)"):
        assert expected in page.text, expected
    assert "media-src 'self'" in page.headers["content-security-policy"]
    audio = h.http.get(f"/api/v1/reports/{slug}/sources/engine_audio/audio", headers=h.headers)
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
    assert hashlib.sha256(audio.content).hexdigest() == rows(h, AudioUpload)[0].raw_sha256
    assert "no-store" in audio.headers["cache-control"] and "noindex" in audio.headers["x-robots-tag"]
    shot = h.http.get(f"/api/v1/reports/{slug}/sources/engine_audio/spectrogram", headers=h.headers)
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png" and shot.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert h.http.get(f"/api/v1/reports/{slug}/sources/engine_audio/other", headers=h.headers).status_code == 404
    assert h.http.get(f"/api/v1/reports/{slug}/sources/engine_audio/audio").status_code == 404  # no authority
    paid = build_paid_report(rig.snapshot())
    assert "spectrogram_sha256" in paid.model_dump_json()


@needs_media
def test_purge_deletes_the_clip_and_the_spectrogram(audio_rig):
    rig = audio_rig
    h = rig.h
    (upload,) = rows(h, AudioUpload)
    keys = [upload.object_key, upload.spectrogram_key]
    assert all(h.storage.head_size(key) for key in keys)
    assert request_deletion(h.db, h.headers["Authorization"].split()[1], h.session_id).state == "deleting"
    queue = JobQueue(h.db)
    lease = queue.claim("purge", ["purge"])
    assert lease is not None and queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)
    assert all(h.storage.head_size(key) is None for key in keys)
    assert rows(h, AudioUpload) == [] and rows(h, EvidenceSource) == []
    with h.db() as db:
        assert db.get(Session, h.session_id).state == SessionState.DELETED


@needs_media
def test_entitlement_revocation_stops_media(audio_rig):
    rig = audio_rig
    h = rig.h
    slug = rows(h, Report)[0].share_slug
    _, entitlement_id = grant(rig, revoked=True)
    assert h.http.get(f"/api/v1/reports/{slug}/sources/engine_audio/audio", headers=h.headers).status_code == 404
