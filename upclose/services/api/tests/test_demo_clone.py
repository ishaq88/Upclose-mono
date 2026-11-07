"""T10b clone/reset machinery: synthetic fixtures only, zero provider calls."""
import http.client
import json
import re
import socket
import urllib.request
from pathlib import Path

import pytest

from app.demo.clone import (
    CloneError,
    NotACloneError,
    clone_dataset,
    list_clones,
    reset_all_clones,
    reset_clone,
    tree_hash,
)

REV = "00000000-0000-4000-8000-000000000007"
FINDING = "00000000-0000-4000-8000-000000000004"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _make_original(root: Path, provenance: str = "synthetic") -> Path:
    original = root / "original"
    original.mkdir(parents=True)
    (original / "manifest.json").write_text(json.dumps({
        "provenance": provenance, "revision_id": REV, "model_requests": 0,
    }, sort_keys=True), encoding="utf-8")
    (original / "report.json").write_text(json.dumps({
        "metadata": {"provenance": provenance},
        "data": {"revision_id": REV, "findings": [{"id": FINDING, "revision_id": REV}]},
    }, sort_keys=True), encoding="utf-8")
    (original / "frames").mkdir()
    (original / "frames" / "frame.bin").write_bytes(b"\x00synthetic-frame\xff")
    return original


def _normalized(root: Path) -> str:
    parts = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == "clone-manifest.json":
            continue
        text = path.read_bytes().decode("utf-8", errors="surrogateescape")
        parts.append(path.relative_to(root).as_posix() + "=" + _UUID.sub("<uuid>", text))
    return "\n".join(parts)


def _no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("clone/reset attempted network/provider transport")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def test_clone_preserves_original_and_labels_provenance(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    original = _make_original(tmp_path)
    before_hash = tree_hash(original)
    before_bytes = {p.relative_to(original).as_posix(): p.read_bytes()
                    for p in original.rglob("*") if p.is_file()}
    clone = clone_dataset(original, tmp_path / "clones")
    assert tree_hash(original) == before_hash
    assert {p.relative_to(original).as_posix(): p.read_bytes()
            for p in original.rglob("*") if p.is_file()} == before_bytes
    marker = json.loads((clone / "clone-manifest.json").read_text())
    assert marker["is_clone"] is True
    assert marker["provenance"] == "synthetic"
    assert marker["is_fresh_live_inference"] is False
    assert marker["model_requests"] == 0
    assert "not fresh live inference" in marker["presentation"]
    assert marker["source_tree_sha256"] == before_hash
    assert (clone / "frames" / "frame.bin").read_bytes() == b"\x00synthetic-frame\xff"


def test_clone_gets_fresh_identifiers(tmp_path):
    original = _make_original(tmp_path)
    clone = clone_dataset(original, tmp_path / "clones")
    cloned = (clone / "report.json").read_text()
    assert REV not in cloned and FINDING not in cloned
    ids = _UUID.findall(cloned)
    assert len(ids) == 3 and len(set(ids)) == 2  # revision reused consistently


def test_clone_independence_mutate_and_delete(tmp_path):
    original = _make_original(tmp_path)
    before = tree_hash(original)
    clone = clone_dataset(original, tmp_path / "clones")
    (clone / "report.json").write_text("tampered", encoding="utf-8")
    (clone / "frames" / "frame.bin").write_bytes(b"changed")
    assert tree_hash(original) == before
    reset_clone(clone)
    assert not clone.exists()
    assert tree_hash(original) == before


def test_reset_refuses_original(tmp_path):
    original = _make_original(tmp_path)
    with pytest.raises(NotACloneError):
        reset_clone(original)
    assert original.is_dir() and (original / "report.json").is_file()
    with pytest.raises(NotACloneError):
        reset_clone(tmp_path / "missing")


def test_repeated_cycles_converge(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    original = _make_original(tmp_path)
    clones_root = tmp_path / "clones"
    first = clone_dataset(original, clones_root)
    first_norm = _normalized(first)
    reset_clone(first)
    second = clone_dataset(original, clones_root)
    assert _normalized(second) == first_norm
    assert second != first
    reset_clone(second)
    third = clone_dataset(original, clones_root)
    assert _normalized(third) == first_norm
    assert list_clones(clones_root) == [third]
    assert reset_all_clones(clones_root) == 1
    assert list_clones(clones_root) == []
    assert tree_hash(original)  # original still intact


def test_clone_refuses_live_and_unknown_provenance(tmp_path):
    live = _make_original(tmp_path / "livecase", provenance="live")
    with pytest.raises(CloneError):
        clone_dataset(live, tmp_path / "clones")
    assert not (tmp_path / "clones").exists() or not list(tmp_path.glob("clones/clone-*"))
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "data.bin").write_bytes(b"x")
    with pytest.raises(CloneError):
        clone_dataset(bare, tmp_path / "clones2")


def test_cached_provenance_accepted_and_never_relabeled_live(tmp_path):
    original = _make_original(tmp_path, provenance="cached")
    clone = clone_dataset(original, tmp_path / "clones")
    marker = json.loads((clone / "clone-manifest.json").read_text())
    assert marker["provenance"] == "cached"
    assert "cached" in (clone / "report.json").read_text()
    assert "live" not in marker["provenance"]
