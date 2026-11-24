import asyncio

from staff.config import Config
from staff.local import Local

MEMBERS = {"fleet": "servers and hosts", "chief": "everything else"}


def make_cfg(**kw):
    base = dict(
        homeserver="h", room_id="r", tom="t", alerts_user="a",
        db_path="d", memory_dir="m", members_dir="x",
        ollama_url="http://ollama.invalid", decision_model="dm",
    )
    base.update(kw)
    return Config(**base)


class FakeResp:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, status=200, payload=None, exc=None):
        self.status, self.payload, self.exc = status, payload, exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return FakeResp(self.status, self.payload)


def answer(choice, confidence):
    return {"answers": {"owner": {"type": "choice", "choice": choice, "confidence": confidence}}}


async def test_embed_success_truncates():
    s = FakeSession(payload={"embeddings": [[0.1, 0.2]]})
    assert await Local(make_cfg(), s).embed("x" * 9000) == [0.1, 0.2]
    url, body = s.calls[0]
    assert url == "http://ollama.invalid/api/embed"
    assert body["model"] == "bge-m3" and len(body["input"]) == 4000


async def test_embed_none_on_timeout():
    assert await Local(make_cfg(), FakeSession(exc=asyncio.TimeoutError())).embed("hi") is None


async def test_embed_none_on_http_500():
    s = FakeSession(status=500, payload={"error": "boom"})
    assert await Local(make_cfg(), s).embed("hi") is None


async def test_embed_none_without_url():
    s = FakeSession(payload={"embeddings": [[1.0]]})
    assert await Local(make_cfg(ollama_url=""), s).embed("hi") is None
    assert s.calls == []


async def test_route_confident():
    s = FakeSession(payload=answer("fleet", 0.83))
    assert await Local(make_cfg(), s).route("disk full", MEMBERS) == "fleet"
    url, body = s.calls[0]
    assert url.endswith("/v1/systemone") and body["model"] == "dm"
    assert set(body["questions"]["owner"]["criteria"]) == {"fleet", "chief", "none"}


async def test_route_low_confidence():
    s = FakeSession(payload=answer("fleet", 0.3))
    assert await Local(make_cfg(), s).route("hm", MEMBERS) is None


async def test_route_none_choice():
    s = FakeSession(payload=answer("none", 0.99))
    assert await Local(make_cfg(), s).route("hm", MEMBERS) is None


async def test_route_no_decision_model():
    s = FakeSession(payload=answer("fleet", 0.9))
    assert await Local(make_cfg(decision_model=""), s).route("hm", MEMBERS) is None
    assert s.calls == []
