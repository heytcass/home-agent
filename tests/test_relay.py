import asyncio
import json
from datetime import datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from staff import relay

REAL_KEY = "sk-ant-real-key"
TOKEN = "relay-token-fleet"
USAGE = {"input_tokens": 1000, "output_tokens": 500}  # $0.014 on claude-opus-5-5
BODY = {"model": "claude-opus-5-5", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}


def sse(*events):
    return b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in events)


def stream_events(model="claude-opus-5-5", usage=None, output=500):
    usage = usage or {"input_tokens": 1000, "output_tokens": 1}
    return [{"type": "message_start", "message": {"id": "msg_1", "model": model, "usage": usage}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": output}},
            {"type": "message_stop"}]


class Upstream:
    """A fake api.anthropic.com. `reply` decides what each call gets."""

    def __init__(self):
        self.calls = []
        self.gate = None  # an asyncio.Event to hold replies back
        self.reply = lambda request: web.json_response({"model": "claude-opus-5-5", "usage": USAGE})

    async def handle(self, request):
        self.calls.append({"path": request.path, "query": request.query_string,
                           "headers": dict(request.headers), "body": await request.read()})
        if self.gate:
            await self.gate.wait()
        reply = self.reply(request)
        return await reply if asyncio.iscoroutine(reply) else reply


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 23, 12, 0)

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def upstream():
    up = Upstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", up.handle)
    server = TestServer(app)
    await server.start_server()
    up.url = str(server.make_url("")).rstrip("/")
    yield up
    await server.close()


@pytest.fixture
def cfg(tmp_path, upstream):
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "fleet").write_text(TOKEN + "\n")
    (tmp_path / "tokens" / "desk").write_text("relay-token-desk\n")
    (tmp_path / "key.env").write_text(f"ANTHROPIC_API_KEY={REAL_KEY}\n")
    return {"listen_host": "127.0.0.1", "port": 0, "upstream": upstream.url,
            "key_file": str(tmp_path / "key.env"), "tokens_dir": str(tmp_path / "tokens"),
            "state_path": str(tmp_path / "spend.json"), "daily_cap_usd": 5.0, "monthly_cap_usd": 50.0}


@pytest.fixture
async def start(cfg, clock):
    clients = []

    async def _start(**overrides):
        client = TestClient(TestServer(relay.make_app({**cfg, **overrides}, now=clock)))
        await client.start_server()
        clients.append(client)
        return client

    yield _start
    for c in clients:
        await c.close()


def post(client, body=BODY, token=TOKEN, path="/v1/messages", **headers):
    return client.post(path, json=body, headers={"x-api-key": token, **headers})


def state(cfg):
    return json.loads(open(cfg["state_path"]).read())


def test_key_file_forms(tmp_path):
    for text in ("sk-bare\n", "ANTHROPIC_API_KEY=sk-bare\n", '# c\nexport ANTHROPIC_API_KEY="sk-bare"\n'):
        (tmp_path / "k").write_text(text)
        assert relay.read_key(str(tmp_path / "k")) == "sk-bare"


def test_prices():
    assert relay.rates("claude-opus-5-5-20260901") == relay.PRICES["claude-opus-5-5"]
    assert relay.rates("claude-fable-5-2") == (8.0, 40.0, 0.20)
    assert relay.rates("claude-fable-5") == (10.0, 50.0, 1.0)
    assert relay.rates("claude-opus-55") == relay.HIGHEST == (10.0, 50.0, 1.0)
    assert relay.rates(None) == relay.HIGHEST
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000,
             "cache_creation_input_tokens": 3000,
             "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 1000}}
    # 0.004 in + 0.010 out + 0.0004 read + 0.005 (5m) + 0.008 (1h) + 0.008 (unattributed, at 1h)
    assert relay.cost(usage, ["claude-opus-5-5"]) == pytest.approx(0.0354)
    assert relay.cost(USAGE, ["claude-opus-5-5"], fast=True) == pytest.approx(0.028)
    assert relay.cost(USAGE, ["claude-haiku-5-5"]) == pytest.approx(0.00035)
    assert relay.cost({**USAGE, "input_tokens": 200_000}, ["claude-haiku-5-5"]) == pytest.approx(0.10125)
    its = {**USAGE, "iterations": [dict(USAGE, model="claude-fable-5-2"), USAGE]}
    assert relay.cost(its, ["claude-opus-5-5"]) == pytest.approx(0.028 + 0.014)


async def test_token_swap_and_header_hygiene(start, upstream, cfg):
    client = await start()
    r = await client.post("/v1/messages?beta=true", json=BODY, headers={
        "Authorization": f"Bearer {TOKEN}", "anthropic-version": "2023-06-01", "anthropic-beta": "b1",
        "Cookie": "x=1", "X-Forwarded-For": "1.2.3.4", "Proxy-Authorization": "p"})
    assert r.status == 200
    assert (await r.json())["usage"] == USAGE
    (call,) = upstream.calls
    sent = {k.lower(): v for k, v in call["headers"].items()}
    assert sent["x-api-key"] == REAL_KEY
    assert sent["anthropic-version"] == "2023-06-01" and sent["anthropic-beta"] == "b1"
    assert sent["content-type"] == "application/json"
    assert not {"authorization", "cookie", "x-forwarded-for", "proxy-authorization"} & set(sent)
    assert TOKEN not in json.dumps(call["headers"])
    assert call["query"] == "beta=true" and json.loads(call["body"]) == BODY
    assert REAL_KEY not in json.dumps(dict(r.headers)) + open(cfg["state_path"]).read()


async def test_unknown_token_is_401(start, upstream, tmp_path):
    (tmp_path / "tokens" / "empty").write_text("\n")
    client = await start()
    for headers in ({"x-api-key": "nope"}, {"x-api-key": REAL_KEY}, {"Authorization": "Bearer "}, {}):
        r = await client.post("/v1/messages", json=BODY, headers=headers)
        assert r.status == 401
        assert (await r.json())["error"]["type"] == "authentication_error"
    assert upstream.calls == []


async def test_tokens_are_reread(start, upstream, tmp_path):
    client = await start()
    assert (await post(client, token="new-token")).status == 401
    (tmp_path / "tokens" / "chief").write_text("new-token")
    assert (await post(client, token="new-token")).status == 200


async def test_other_paths_are_404(start, upstream, caplog):
    client = await start()
    for method, path in (("GET", "/v1/messages"), ("POST", "/v1/messages/batches"), ("GET", "/v1/models"),
                         ("GET", "/spend"), ("POST", "/v1/messages/")):
        r = await client.request(method, path, headers={"x-api-key": TOKEN, "x-secret": "hush"})
        assert r.status == 404
    assert upstream.calls == []
    assert "404 POST /v1/messages/batches caller=fleet" in caplog.text
    assert "hush" not in caplog.text and TOKEN not in caplog.text


async def test_streaming_passes_through_unbuffered_and_is_costed(start, upstream, cfg):
    first, *rest = stream_events(usage={"input_tokens": 1000, "output_tokens": 1, "cache_read_input_tokens": 2000})
    release = asyncio.Event()

    async def reply(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "request-id": "req_1"})
        await resp.prepare(request)
        await resp.write(sse(first))
        await release.wait()
        tail = sse(*rest)
        await resp.write(tail[:40])  # an event split across chunks
        await resp.write(tail[40:])
        return resp

    upstream.reply = reply
    client = await start()
    r = await post(client, {**BODY, "stream": True})
    assert r.status == 200 and r.headers["Content-Type"] == "text/event-stream" and r.headers["request-id"] == "req_1"
    got = await asyncio.wait_for(r.content.readuntil(b"\n\n"), 2)  # arrives while upstream is still open
    assert b"message_start" in got
    release.set()
    assert got + await r.read() == sse(first, *rest)
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(0.004 + 0.0004 + 0.010)


async def test_non_streaming_is_costed_per_caller(start, upstream, cfg):
    client = await start()
    await post(client)
    await post(client)
    await post(client, token="relay-token-desk")
    s = state(cfg)
    assert s["days"]["2026-11-23"] == {"total": pytest.approx(0.042), "callers": {"fleet": pytest.approx(0.028), "desk": pytest.approx(0.014)}}
    assert s["months"]["2026-11"]["total"] == pytest.approx(0.042)


async def test_count_tokens_and_errors_cost_nothing(start, upstream, cfg):
    client = await start()
    upstream.reply = lambda request: web.json_response({"input_tokens": 12})
    r = await post(client, path="/v1/messages/count_tokens")
    assert await r.json() == {"input_tokens": 12}
    upstream.reply = lambda request: web.json_response({"type": "error"}, status=529, headers={"retry-after": "3"})
    r = await post(client)
    assert r.status == 529 and r.headers["retry-after"] == "3"
    assert [c["path"] for c in upstream.calls] == ["/v1/messages/count_tokens", "/v1/messages"]
    assert client.app[relay.SPEND].days == {} and client.app[relay.SPEND].inflight == 0


async def test_unknown_model_pays_the_highest_rate(start, upstream, cfg):
    upstream.reply = lambda request: web.json_response({"model": "claude-next-9", "usage": USAGE})
    client = await start()
    await post(client, {**BODY, "model": "claude-next-9"})
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(0.010 + 0.025)


async def test_pricier_served_model_is_the_one_charged(start, upstream, cfg):
    upstream.reply = lambda request: web.json_response({"model": "claude-fable-5-2", "usage": USAGE})
    client = await start()
    await post(client, {**BODY, "model": "claude-haiku-5-5"})
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(0.008 + 0.020)


async def test_cap_reached_is_429_and_upstream_not_called(start, upstream):
    client = await start(daily_cap_usd=0.02)
    assert (await post(client, {**BODY, "max_tokens": 10})).status == 200  # $0.014 spent
    assert (await post(client, {**BODY, "max_tokens": 10})).status == 200  # $0.028 spent: over
    r = await post(client, {**BODY, "max_tokens": 10})
    assert r.status == 429 and "retry-after" not in r.headers and r.headers["x-should-retry"] == "false"
    err = await r.json()
    assert err["type"] == "error" and err["error"]["type"] == "rate_limit_error"
    assert "daily spend cap of $0.02 is reached" in err["error"]["message"]
    assert (await post(client, path="/v1/messages/count_tokens")).status == 429
    assert len(upstream.calls) == 2


async def test_monthly_cap_names_itself(start, upstream):
    client = await start(monthly_cap_usd=0.01)
    assert (await post(client, {**BODY, "max_tokens": 10})).status == 200
    r = await post(client, {**BODY, "max_tokens": 10})
    assert r.status == 429 and "monthly spend cap" in (await r.json())["error"]["message"]


async def test_concurrent_requests_cannot_jointly_exceed_the_cap(start, upstream, cfg):
    # Each request reserves about $0.02 (1000 output tokens on opus 5.5) and really costs $0.02.
    upstream.reply = lambda request: web.json_response({"model": "claude-opus-5-5", "usage": {"input_tokens": 0, "output_tokens": 1000}})
    upstream.gate = asyncio.Event()
    client = await start(daily_cap_usd=0.05)
    tasks = [asyncio.create_task(post(client)) for _ in range(8)]
    while len(upstream.calls) < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert len(upstream.calls) == 2  # a third would not fit under the cap
    upstream.gate.set()
    statuses = sorted([(await t).status for t in tasks])
    assert statuses == [200, 200] + [429] * 6
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(0.04)
    assert client.app[relay.SPEND].inflight == pytest.approx(0)


async def test_missing_usage_charges_the_assumed_amount(start, upstream, cfg):
    client = await start()
    body = {**BODY, "max_tokens": 2000}
    expect = relay.assumed(body, len(json.dumps(body)))
    assert expect == pytest.approx((len(json.dumps(body)) // 3 + 1) * 4e-6 + 0.04, rel=0.01)

    upstream.reply = lambda request: web.Response(text="not json", content_type="application/json")
    await post(client, body)
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(expect, rel=0.01)

    async def cut(request):  # a stream that dies after message_start
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(sse(stream_events()[0]))
        request.transport.close()
        return resp

    upstream.reply = cut
    try:
        await (await post(client, {**body, "stream": True})).read()
    except Exception:
        pass
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(2 * expect, rel=0.01)
    assert client.app[relay.SPEND].inflight == pytest.approx(0)


async def test_request_without_max_tokens_reserves_the_maximum(start, upstream):
    client = await start()
    r = await post(client, {"model": "claude-fable-5-2", "messages": []})  # 128K output at $40 > $5 cap
    assert r.status == 429 and "no room" in (await r.json())["error"]["message"]
    assert (await client.post("/v1/messages", data=b"[1]", headers={"x-api-key": TOKEN})).status == 400
    assert upstream.calls == []


@pytest.mark.parametrize("content", ["", "{not json", '{"days": {}}', '{"days": {"2026-11-23": {"total": -5, "callers": {}}}, "months": {}}'])
async def test_corrupt_state_refuses_everything(start, upstream, cfg, content, caplog):
    open(cfg["state_path"], "w").write(content)
    client = await start()
    for path in ("/v1/messages", "/v1/messages/count_tokens"):
        r = await post(client, path=path)
        assert r.status == 503 and (await r.json())["type"] == "error"
    assert upstream.calls == []
    assert open(cfg["state_path"]).read() == content  # left for a human to look at
    assert "unusable" in caplog.text
    open(cfg["state_path"], "w").write('{"days": {}, "months": {}}')  # the human fixed it
    assert (await post(client)).status == 200


async def test_unwritable_state_refuses_until_it_can_be_written(start, upstream, cfg, tmp_path, monkeypatch):
    client = await start()
    real = relay.os.replace
    monkeypatch.setattr(relay.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    assert (await post(client)).status == 200  # spent, but could not be written down
    assert (await post(client)).status == 503
    monkeypatch.setattr(relay.os, "replace", real)
    assert (await post(client)).status == 200
    assert state(cfg)["days"]["2026-11-23"]["total"] == pytest.approx(0.028)


async def test_state_survives_a_restart(start, upstream, cfg):
    first = await start(daily_cap_usd=0.02)
    for _ in range(2):
        await post(first, {**BODY, "max_tokens": 10})
    await first.close()
    second = await start(daily_cap_usd=0.02)
    assert second.app[relay.SPEND].days["2026-11-23"]["total"] == pytest.approx(0.028)
    assert (await post(second, {**BODY, "max_tokens": 10})).status == 429
    assert len(upstream.calls) == 2


async def test_day_rollover_and_pruning(start, upstream, cfg, clock):
    client = await start(daily_cap_usd=0.02, monthly_cap_usd=0.05)
    small = {**BODY, "max_tokens": 10}
    for _ in range(2):
        await post(client, small)
    assert (await post(client, small)).status == 429
    clock.t += timedelta(days=1)
    assert (await post(client, small)).status == 200  # a new day
    s = state(cfg)
    assert s["days"]["2026-11-23"]["total"] == pytest.approx(0.028)
    assert s["days"]["2026-11-24"]["total"] == pytest.approx(0.014)
    assert s["months"]["2026-11"]["total"] == pytest.approx(0.042)
    clock.t += timedelta(days=1)
    await post(client, small)  # $0.056 this month: over the monthly cap
    clock.t += timedelta(days=1)
    r = await post(client, small)
    assert r.status == 429 and "monthly" in (await r.json())["error"]["message"]
    clock.t += timedelta(days=70)  # February: new month, old days pruned on the next write
    assert (await post(client, small)).status == 200
    s = state(cfg)
    assert list(s["days"]) == ["2027-02-04"] and set(s["months"]) == {"2026-11", "2027-02"}
