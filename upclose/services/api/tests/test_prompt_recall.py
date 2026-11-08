"""Pins the SAM concept prompt wording that live runs showed to produce candidates on synthetic twin renders.

Bare singular nouns ("scratch", "rust", "corroded metal") returned an empty SAM output on those clips, while
plural or descriptive noun phrases tracked the damage. SAM proposes with high recall; verification is the
precision gate, so a silent empty proposal must not come from wording alone.
"""
from app.pipeline.orchestration import load_pipeline_config


def test_damage_prompts_use_phrasing_that_returns_candidates() -> None:
    texts = {row.prompt_id: row.text for row in load_pipeline_config().prompts.prompts}
    assert texts["scratch"] == "scratches"
    assert texts["rust_a"] == "rust patch"
    assert texts["rust_b"] == "rust spots"
    assert {"scratch", "rust", "corroded metal"}.isdisjoint(texts.values())


def test_dash_cluster_prompt_is_not_the_phrase_that_returned_empty_output() -> None:
    texts = {row.prompt_id: row.text for row in load_pipeline_config().prompts.prompts}
    # "instrument panel" and "instrument cluster" returned an empty SAM output on a twin dash still;
    # "dashboard gauges" returned a box.
    assert texts["dash_cluster"] == "dashboard gauges"
