"""Reader adapters: fixture, Meta transport (mocked), provider selection, key hygiene."""
import io
import json

import pytest
from PIL import Image

from app.adapters.dash_reader import (DashReaderError, FixtureDashReader, GeminiDashReader, accept,
                                      build_reader)
from app.adapters.dash_reader.meta import MODEL, MetaDashReader
from app.adapters.meta import ProviderFailure

SECRET = "dash-test-secret-must-never-appear"
GOOD = ('{"cluster_readable":true,"engine_running_evidence":"tach_above_zero",'
        '"lit_indicators":[{"indicator":"oil_pressure","confidence":5}],"notes":"synthetic"}')


def image() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 10, 10)).save(out, format="PNG")
    return out.getvalue()


def envelope(text, status="completed", model=MODEL):
    return json.dumps({"status": status, "model": model, "usage": {"input_tokens": 100, "output_tokens": 20},
                       "output": [{"type": "reasoning", "content": []},
                                  {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": text}]}]}).encode()


def reader(responder, calls=None):
    def transport(url, body, headers, timeout):
        if calls is not None:
            calls.append((url, body, headers))
        return responder()
    return MetaDashReader(api_key=SECRET, transport=transport)


def test_fixture_reader_valid_and_malformed():
    out = FixtureDashReader(GOOD).read(image())
    assert out.reading.lit_indicators[0].indicator == "oil_pressure" and out.provenance == "synthetic"
    bad = FixtureDashReader("{oops").read(image())
    assert bad.reading is None and bad.reject_code.startswith("invalid_response")


def test_meta_success_request_shape_and_usage():
    calls = []
    out = reader(lambda: (200, envelope(GOOD)), calls).read(image(), grant=lambda bound: bound > 0)
    assert out.reading is not None and (out.input_tokens, out.output_tokens) == (100, 20)
    url, body, headers = calls[0]
    request = json.loads(body)
    assert url.endswith("/v1/responses") and request["model"] == MODEL and request["store"] is False
    content = request["input"][0]["content"]
    assert [c["type"] for c in content] == ["input_text", "input_image"]
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert request["text"]["format"]["strict"] is True


def test_meta_requires_budget_grant_before_sending():
    calls = []
    r = reader(lambda: (200, envelope(GOOD)), calls)
    for grant in (None, lambda b: False, lambda b: 1 / 0):
        with pytest.raises(ProviderFailure) as info:
            r.read(image(), grant=grant)
        assert info.value.code == "budget_not_reserved"
    assert calls == []


@pytest.mark.parametrize("text", ["{not json", "[]", GOOD.replace("oil_pressure", "unknown_light")])
def test_meta_billed_invalid_output_is_not_readable_not_retried(text):
    out = reader(lambda: (200, envelope(text))).read(image(), grant=lambda b: True)
    assert out.reading is None and out.reject_code.startswith("invalid_response")
    assert out.input_tokens == 100  # billed usage is still reported for settlement


def test_meta_incomplete_and_wrong_model_and_missing_text():
    r = reader(lambda: (200, envelope(GOOD, status="incomplete")))
    assert r.read(image(), grant=lambda b: True).reject_code == "invalid_response:incomplete"
    r = reader(lambda: (200, envelope(GOOD, model="other-model")))
    assert r.read(image(), grant=lambda b: True).reading is None
    r = reader(lambda: (200, json.dumps({"status": "completed", "model": MODEL, "output": []}).encode()))
    assert r.read(image(), grant=lambda b: True).reject_code == "invalid_response:no_text"


@pytest.mark.parametrize("status,code", [(401, "standard_access_unverified"), (429, "provider_timeout"),
                                         (503, "provider_timeout"), (400, "provider_failed")])
def test_meta_http_failures_are_stable_codes_without_key(status, code):
    with pytest.raises(ProviderFailure) as info:
        reader(lambda: (status, b"")).read(image(), grant=lambda b: True)
    assert info.value.code == code and SECRET not in repr(info.value) and SECRET not in str(info.value)


def test_meta_bad_image_fails_closed_without_key():
    with pytest.raises(ProviderFailure) as info:
        reader(lambda: (200, envelope(GOOD))).read(b"not an image", grant=lambda b: True)
    assert info.value.code == "malformed_record" and SECRET not in str(info.value)


def test_build_reader_selection_and_key_hygiene():
    assert isinstance(build_reader({}), FixtureDashReader)
    assert isinstance(build_reader({"DASH_READER_PROVIDER": "meta", "MODEL_API_KEY": SECRET}), MetaDashReader)
    with pytest.raises(DashReaderError) as info:
        build_reader({"DASH_READER_PROVIDER": "meta"})
    assert SECRET not in str(info.value)
    with pytest.raises(DashReaderError):
        build_reader({"DASH_READER_PROVIDER": "meta", "MODEL_API_KEY": SECRET, "META_API_BASE": "http://x"})
    with pytest.raises(DashReaderError):
        build_reader({"DASH_READER_PROVIDER": "gemini", "GEMINI_API_KEY": SECRET})
    with pytest.raises(DashReaderError):
        build_reader({"DASH_READER_PROVIDER": "nope"})
    with pytest.raises(DashReaderError):
        GeminiDashReader()


def test_reader_reprs_hide_key():
    assert SECRET not in repr(MetaDashReader(api_key=SECRET).__dict__.get("_base"))
    assert accept(GOOD, provider="x", model="y", provenance="synthetic").reading is not None
