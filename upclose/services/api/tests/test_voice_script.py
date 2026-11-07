import json
from pathlib import Path

import pytest

from app.domain.voice_script import (VoiceConfig, VoiceLine, VoiceScriptError, build_script, lines_from_json,
                                     load_voice_config, speak_numbers)

CFG = VoiceConfig("v", 400, "This audio is AI-generated from your report.")
DISCLOSURE = CFG.disclosure


def test_numbers_spoken_naturally():
    assert speak_numbers("Repairs cost $1,200 to $3,450.50") == "Repairs cost 1,200 dollars to 3,450 dollars and 50 cents"
    assert speak_numbers("Score 7.5") == "Score 7 point 5"


def test_build_script_short_sentences_and_disclosure_last():
    script = build_script(["Headline score is 8.2", "One finding is present."], CFG)
    assert script == f"Headline score is 8 point 2. One finding is present. {DISCLOSURE}"


def test_simulated_prefix():
    script = build_script([VoiceLine("Rig reading ok", True), VoiceLine("Real photo ok")], CFG)
    assert "Simulated: Rig reading ok. Real photo ok." in script
    assert "Simulated: Real photo" not in script
    assert "Simulated: " in build_script(["a"], CFG, simulated=True)


def test_cap_drops_whole_statements_and_keeps_disclosure():
    script = build_script(["x" * 150] * 10, CFG)
    assert len(script) <= CFG.max_chars
    assert script.endswith(DISCLOSURE)
    assert script.count("x" * 150) == 2


def test_disclosure_always_present_even_empty():
    assert build_script([], CFG) == DISCLOSURE
    assert build_script(["  "], CFG) == DISCLOSURE


def test_lines_from_json_and_errors():
    assert lines_from_json({"statements": [{"text": "a", "simulated": True}, "b"]}) == (VoiceLine("a", True), VoiceLine("b"))
    with pytest.raises(VoiceScriptError):
        lines_from_json({"nope": 1})
    with pytest.raises(VoiceScriptError):
        lines_from_json([1])


def test_shipped_config_loads():
    cfg = load_voice_config(Path(__file__).resolve().parents[1] / "config" / "voice.v1.json")
    assert cfg.max_chars > 0 and cfg.disclosure == DISCLOSURE


def test_config_rejects_tiny_cap(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"script_version": "v", "max_chars": 10, "disclosure": DISCLOSURE}))
    with pytest.raises(VoiceScriptError):
        load_voice_config(f)
