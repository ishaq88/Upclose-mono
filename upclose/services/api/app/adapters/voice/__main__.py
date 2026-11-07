"""CLI: python -m app.adapters.voice NARRATION.json OUTPUT [--simulated]

Prints only the output path, provenance and script length; never the key or the text.
"""

import argparse
import json
import sys
from pathlib import Path

from app.domain.voice_script import VoiceScriptError, build_script, lines_from_json, load_voice_config

from . import VoiceProviderError, get_voice_adapter
from .config import VoiceConfigError

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "voice.v1.json"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.adapters.voice")
    p.add_argument("narration_json")
    p.add_argument("output", help="output file; extension is set from the provider's format")
    p.add_argument("--simulated", action="store_true", help="prefix every statement with 'Simulated:'")
    args = p.parse_args(argv)
    try:
        config = load_voice_config(CONFIG_PATH)
        raw = json.loads(Path(args.narration_json).read_text(encoding="utf-8"))
        script = build_script(lines_from_json(raw), config, simulated=args.simulated)
        audio = get_voice_adapter().synthesize(script)
    except (VoiceScriptError, VoiceConfigError, VoiceProviderError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = Path(args.output).with_suffix("." + audio.extension)
    out.write_bytes(audio.data)
    print(f"wrote {out} provenance={audio.provenance} script_chars={len(script)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
