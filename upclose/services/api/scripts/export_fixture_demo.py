"""Export a fresh synthetic offline rehearsal (never real media or live inference)."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.report.offline import export_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scope', choices=('free', 'paid'), default='free')
    args = parser.parse_args()
    path = export_fixture(args.output, args.scope)
    print(f'Synthetic offline rehearsal written to {path}')


if __name__ == '__main__':
    main()
