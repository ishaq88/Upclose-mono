"""Generic cached-demo clone/reset for synthetic/cached datasets (T10b).

A dataset is a directory tree (report JSON, artifacts, manifest). Clone copies
it to a fresh directory with fresh UUID identifiers and explicit provenance
labeling; the original is never opened for writing and its tree hash is
verified before and after. Reset deletes only directories carrying a valid
clone marker and refuses originals. This module imports only the standard
library: clone/reset/replay paths make zero model/provider/transport calls.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4

ALLOWED_PROVENANCE = frozenset({"synthetic", "cached"})
CLONE_MARKER = "clone-manifest.json"
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CloneError(ValueError):
    """Source cannot be cloned (missing, live/unknown provenance, unsafe)."""


class NotACloneError(CloneError):
    """Reset target is not a clone; refusing to delete or mutate an original."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hash(root: Path) -> str:
    """Deterministic hash over sorted (relative path, file hash) pairs."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(_sha256(path.read_bytes()).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        if path.is_symlink() or root.joinpath(path.relative_to(root)).is_symlink():
            raise CloneError("symlinks are not supported in demo datasets")
    return files


def _provenance_of(source: Path, files: list[Path]) -> str:
    manifest = source / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CloneError("source manifest.json is not valid JSON") from exc
        provenance = data.get("provenance") if isinstance(data, dict) else None
        if provenance is not None:
            return str(provenance)
    for path in files:
        if path.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            metadata = data.get("metadata")
            if isinstance(metadata, dict) and metadata.get("provenance"):
                return str(metadata["provenance"])
    raise CloneError("source dataset has no provenance; refusing to label it")


def _collect_uuids(value: object, found: set[str]) -> None:
    if isinstance(value, str):
        if _UUID_RE.match(value):
            found.add(value.lower())
    elif isinstance(value, dict):
        for item in value.values():
            _collect_uuids(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_uuids(item, found)


def _rewrite_uuids(value: object, mapping: dict[str, str]) -> object:
    if isinstance(value, str):
        return mapping.get(value.lower(), value)
    if isinstance(value, dict):
        return {key: _rewrite_uuids(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_uuids(item, mapping) for item in value]
    return value


def clone_dataset(
    source: Path,
    clones_root: Path,
    *,
    new_id: Callable[[], UUID] = uuid4,
) -> Path:
    """Create a fresh labeled clone of a synthetic/cached dataset directory.

    The original is only read; its tree hash must be identical after cloning.
    Every UUID string inside cloned JSON files is remapped to a fresh UUID
    (consistently across files). Binary artifacts are copied byte-for-byte.
    """
    source = Path(source)
    clones_root = Path(clones_root)
    if not source.is_dir():
        raise CloneError("source dataset directory does not exist")
    if source.resolve() == clones_root.resolve():
        raise CloneError("clones_root must differ from the source directory")
    files = _files(source)
    provenance = _provenance_of(source, files)
    if provenance not in ALLOWED_PROVENANCE:
        raise CloneError(
            f"refusing to clone provenance={provenance!r}; "
            "only synthetic/cached datasets may be cloned, never fresh live inference"
        )

    before = tree_hash(source)

    # Build one consistent old->new UUID mapping across all JSON payloads.
    mapping: dict[str, str] = {}
    parsed: dict[Path, object] = {}
    for path in files:
        if path.suffix.lower() != ".json" or path.name == CLONE_MARKER:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        parsed[path] = data
        found: set[str] = set()
        _collect_uuids(data, found)
        for old in sorted(found):
            if old not in mapping:
                mapping[old] = str(new_id())

    clone_id = new_id()
    clones_root.mkdir(parents=True, exist_ok=True)
    dest = clones_root / f"clone-{clone_id}"
    dest.mkdir(parents=False, exist_ok=False)
    try:
        for path in files:
            rel = path.relative_to(source)
            if rel.as_posix() == CLONE_MARKER:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if path in parsed:
                rewritten = _rewrite_uuids(parsed[path], mapping)
                target.write_text(
                    json.dumps(rewritten, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            else:
                target.write_bytes(path.read_bytes())
        marker = {
            "schema_version": 1,
            "is_clone": True,
            "clone_id": str(clone_id),
            "provenance": provenance,
            "presentation": f"{provenance} cached-demo clone — not fresh live inference",
            "is_fresh_live_inference": False,
            "model_requests": 0,
            "source_tree_sha256": before,
            "id_remap_count": len(mapping),
        }
        (dest / CLONE_MARKER).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    if tree_hash(source) != before:
        shutil.rmtree(dest, ignore_errors=True)
        raise CloneError("source dataset changed during cloning; clone discarded")
    return dest


def _require_clone(path: Path) -> dict:
    path = Path(path)
    if not path.is_dir():
        raise NotACloneError(f"reset target does not exist or is not a directory: {path}")
    marker_path = path / CLONE_MARKER
    if not marker_path.is_file():
        raise NotACloneError("refusing reset: no clone marker (this looks like an original)")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise NotACloneError("refusing reset: clone marker is not valid JSON") from exc
    if not isinstance(marker, dict) or marker.get("is_clone") is not True:
        raise NotACloneError("refusing reset: marker does not declare a clone")
    if marker.get("provenance") not in ALLOWED_PROVENANCE:
        raise NotACloneError("refusing reset: clone marker has unexpected provenance")
    clone_id = marker.get("clone_id")
    if not isinstance(clone_id, str) or path.name != f"clone-{clone_id}":
        raise NotACloneError("refusing reset: clone marker does not match directory name")
    return marker


def reset_clone(path: Path) -> None:
    """Delete one clone directory. Refuses originals and non-clones."""
    _require_clone(Path(path))
    shutil.rmtree(Path(path))


def list_clones(clones_root: Path) -> list[Path]:
    clones_root = Path(clones_root)
    if not clones_root.is_dir():
        return []
    result = []
    for child in sorted(clones_root.iterdir()):
        if child.is_dir() and (child / CLONE_MARKER).is_file():
            result.append(child)
    return result


def reset_all_clones(clones_root: Path) -> int:
    """Delete every valid clone under clones_root; leave all else untouched."""
    count = 0
    for clone in list_clones(clones_root):
        reset_clone(clone)
        count += 1
    return count
