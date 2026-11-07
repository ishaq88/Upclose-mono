"""Clone/reset a synthetic/cached demo dataset (T10b). No model/provider calls.

Usage:
  python scripts/demo_clone.py clone --source DIR --clones-root DIR
  python scripts/demo_clone.py reset --path DIR
  python scripts/demo_clone.py reset-all --clones-root DIR
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.demo.clone import CloneError, clone_dataset, reset_all_clones, reset_clone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    clone_p = sub.add_parser("clone")
    clone_p.add_argument("--source", type=Path, required=True)
    clone_p.add_argument("--clones-root", type=Path, required=True)
    reset_p = sub.add_parser("reset")
    reset_p.add_argument("--path", type=Path, required=True)
    reset_all_p = sub.add_parser("reset-all")
    reset_all_p.add_argument("--clones-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "clone":
            print(clone_dataset(args.source, args.clones_root))
        elif args.command == "reset":
            reset_clone(args.path)
            print(f"reset clone {args.path}")
        else:
            print(f"reset {reset_all_clones(args.clones_root)} clone(s)")
    except CloneError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
