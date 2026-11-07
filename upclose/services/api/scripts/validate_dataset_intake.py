"""Validate a candidate evaluation dataset (manifest + media) before any evaluation run.

Offline only: reads bounded local JSON and local media files, hashes bytes,
and never calls a provider, network, storage, or database.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from app.evaluation.intake import (
    INTAKE_RECEIPT_VERSION, IntakeManifest, manifest_violations,
    validate_dataset_intake,
)

MAX_INPUT_BYTES = 16 * 1024 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def read_input(path: Path) -> tuple[bytes, object]:
    with path.open('rb') as handle:
        content = handle.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError('input exceeds bound')
    return content, json.loads(content, object_pairs_hook=unique_object)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--media-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    violations: list[str] = []
    try:
        if args.output.resolve() == args.manifest.resolve():
            raise ValueError('output cannot overwrite inputs')
        _, data = read_input(args.manifest)
        try:
            manifest = IntakeManifest.model_validate(data)
        except ValidationError as error:
            violations = manifest_violations(error)
            receipt = {
                'schema_version': INTAKE_RECEIPT_VERSION, 'valid': False,
                'declared_provenance': None, 'provenance_verified': False,
                'counts': {'images': 0, 'walkarounds': 0, 'cars': 0, 'labels': 0,
                           'media_files_checked': 0},
                'violations': violations,
                'gates': {'dataset_intake': 'failed', 'live_quality': 'unverified',
                          'live_feasibility': 'unverified', 'standard_access': 'unverified'},
                'limitations': ['Manifest structure/labels invalid; no media was trusted.'],
            }
        else:
            receipt = validate_dataset_intake(manifest, args.media_root)
            violations = receipt['violations']
        receipt['command'] = {'script': 'scripts/validate_dataset_intake.py',
                              'manifest': str(args.manifest), 'media_root': str(args.media_root),
                              'output': str(args.output)}
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + '\n',
                               encoding='utf-8')
    except (OSError, ValueError):
        print('DATASET_INTAKE_INPUT_INVALID: no intake receipt produced', file=sys.stderr)
        return 2
    if violations:
        for reason in violations:
            print(f'DATASET_INTAKE_INVALID: {reason}', file=sys.stderr)
        return 2
    print('Dataset intake passed; all live/access gates remain unverified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
