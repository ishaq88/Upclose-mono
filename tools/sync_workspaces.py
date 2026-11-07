"""Copy reviewed work from the development checkouts into this monorepo.

Development happens in separate working folders; this repo receives only reviewed snapshots.
Only files Git would track in the source (tracked + untracked-not-ignored) are copied, so ignored
files such as .env, recordings, virtualenvs and node_modules never reach this repo.

    python tools/sync_workspaces.py upclose G:/upclose [--only PATH ...] [--dry-run]
    python tools/sync_workspaces.py upclose-rig G:/upclose-rig [--only PATH ...] [--dry-run]

--only limits the copy (and deletions) to the given source-relative paths or folders.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source_files(source: Path) -> set[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=source,
                         capture_output=True, check=True).stdout.decode("utf-8")
    return {name for name in out.split("\0") if name and (source / name).is_file()}


def within(name: str, only: list[str]) -> bool:
    return not only or any(name == p or name.startswith(p.rstrip("/") + "/") for p in only)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="subfolder of this repo, e.g. upclose")
    parser.add_argument("source", type=Path, help="development checkout")
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    only = [p.replace("\\", "/") for p in args.only]
    dest = ROOT / args.target
    wanted = {name for name in source_files(args.source) if within(name, only)}
    existing = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()} if dest.exists() else set()

    copied = removed = 0
    for name in sorted(wanted):
        src, dst = args.source / name, dest / name
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            continue
        copied += 1
        print("copy  ", name)
        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    for name in sorted(existing - wanted):
        if within(name, only):
            removed += 1
            print("remove", name)
            if not args.dry_run:
                (dest / name).unlink()
    print(f"{copied} copied, {removed} removed{' (dry run)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
