"""Compare captured predictions to permission/label declarations; never calls a provider."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from app.evaluation import DatasetManifest, PredictionBundle, evaluate, input_hash

MAX_INPUT_BYTES = 16 * 1024 * 1024


def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def read_input(path: Path) -> tuple[bytes,object]:
    with path.open('rb') as handle:
        content = handle.read(MAX_INPUT_BYTES+1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError('input exceeds bound')
    return content,json.loads(content,object_pairs_hook=unique_object)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--predictions',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--split',choices=('tuning','heldout'),default='heldout')
    args = parser.parse_args()
    try:
        if args.output.resolve() in (args.dataset.resolve(),args.predictions.resolve()):
            raise ValueError('output cannot overwrite inputs')
        raw_dataset,dataset = read_input(args.dataset)
        raw_predictions,predictions = read_input(args.predictions)
        receipt = evaluate(DatasetManifest.model_validate(dataset),PredictionBundle.model_validate(predictions),
            dataset_sha256=input_hash(raw_dataset),predictions_sha256=input_hash(raw_predictions),split=args.split)
        receipt['command'] = {'script':'scripts/evaluate_predictions.py','split':args.split,
            'dataset':str(args.dataset),'predictions':str(args.predictions),'output':str(args.output)}
        receipt['output_artifact'] = str(args.output)
        args.output.write_text(json.dumps(receipt,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    except (OSError,ValueError,ValidationError):
        # Pydantic errors may include raw private inputs. Emit only a stable failure code.
        print('EVALUATION_INPUT_INVALID: no evaluation receipt produced',file=sys.stderr)
        return 2
    print('Offline receipt written; all live/access/latency/spend gates remain unverified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
