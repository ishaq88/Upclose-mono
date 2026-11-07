"""Export deterministic Pydantic schemas; does not register unimplemented routes."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from app import contracts


def export(*, check: bool = False) -> None:
    models = [
        value for name in contracts.__all__
        if isinstance(value := getattr(contracts, name), type)
        and issubclass(value, BaseModel)
    ]
    _, bundle = models_json_schema([(model, "serialization") for model in models])
    _, openapi_bundle = models_json_schema(
        [(model, "serialization") for model in models],
        ref_template="#/components/schemas/{model}",
    )
    schemas = openapi_bundle["$defs"]
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "UpClose contracts (routes not yet implemented)", "version": "0.1.0"},
        "paths": {},
        "components": {"schemas": schemas},
    }
    target = ROOT / "schema"
    if not check:
        target.mkdir(exist_ok=True)
    for name, document in [("contracts.schema.json", bundle), ("openapi.json", openapi)]:
        content = json.dumps(document, indent=2, sort_keys=True) + "\n"
        path = target / name
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Stale schema: {name}; run scripts/export_contracts.py")
        else:
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed schemas differ; write no files")
    export(check=parser.parse_args().check)
