"""Stitch demo footage snippets into one example video (offline, no provider calls).

Usage:
  python scripts/demo_footage_intake.py --sidecar path/to/sidecar.json --output-dir path/to/out

The sidecar lists ordered snippets with source/car/area labels and start
offsets; provenance is `synthetic` (test fixtures) or `cached` (real footage
with permission_cleared=true). Writes example.mp4 + demo-manifest.json.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.demo.intake import IntakeError, build_demo_video


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-name", default="example.mp4")
    args = parser.parse_args()
    try:
        manifest = build_demo_video(args.sidecar, args.output_dir, video_name=args.output_name)
    except IntakeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
