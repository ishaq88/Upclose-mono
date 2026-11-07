import json
import uuid

import httpx
import pytest

from upclose_rig.post import PostError, post_source, token_from_env

SID = str(uuid.uuid4())


def client(status, payload=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {"detail": "x"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_201_sends_bearer_to_contract_path():
    seen = []
    payload = {"source_id": "s1", "kind": "rig_telemetry", "data_sha256": "ab", "summary": {"k": 1}}
    r = post_source('{"a":1}', "http://api.test/", SID, "tok-123", client(201, payload, seen))
    assert r.ok and r.status == 201 and "s1" in r.message
    req = seen[0]
    assert req.method == "POST" and req.url.path == f"/api/v1/sessions/{SID}/sources"
    assert req.headers["authorization"] == "Bearer tok-123"
    assert json.loads(req.content) == {"a": 1}
    assert "tok-123" not in r.message


@pytest.mark.parametrize("status,word", [(409, "finalized"), (401, "token"), (404, "not found"), (422, "422"), (500, "500")])
def test_error_statuses(status, word):
    r = post_source("{}", "http://api.test", SID, "t", client(status))
    assert not r.ok and r.status == status and word in r.message


def test_network_error_message_has_no_secrets():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    r = post_source("{}", "http://api.test", SID, "secret-token", httpx.Client(transport=httpx.MockTransport(boom)))
    assert not r.ok and r.status is None and "secret-token" not in r.message


def test_bad_session_and_missing_token(monkeypatch):
    with pytest.raises(PostError):
        post_source("{}", "http://x", "not-a-uuid", "t")
    monkeypatch.delenv("UPCLOSE_OWNER_TOKEN", raising=False)
    with pytest.raises(PostError):
        token_from_env()
    monkeypatch.setenv("UPCLOSE_OWNER_TOKEN", " abc ")
    assert token_from_env() == "abc"
