"""The relay: the only process that holds the real Anthropic key.

Callers present a relay token; the relay swaps in the real key, forwards
`POST /v1/messages` and `POST /v1/messages/count_tokens`, counts what each
response cost at list prices, and refuses everything once the daily or
monthly cap is reached. See ARCHITECTURE.md, "Wire 2: the relay".

Every doubtful case is resolved toward charging more or refusing.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("staff.relay")

ROUTES = {"/v1/messages", "/v1/messages/count_tokens"}
MAX_OUTPUT = 128_000  # assumed when a request names no usable max_tokens
MAX_BODY = 64 * 1024 * 1024
KEEP_DAYS = 62

# Dollars per million tokens: (input, output, cache read). Cache writes are
# 1.25x input (5-minute) and 2x input (1-hour) on every model. Source: the
# claude-api skill (cached 2026-11-03), see the report that came with this file.
PRICES = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.50, 2.50, 0.05),  # the card for prompts over 100K tokens
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
HAIKU_55_SHORT = (0.10, 0.50, 0.01)  # prompts of 100K tokens or fewer
HIGHEST = tuple(max(p[i] for p in PRICES.values()) for i in range(3))
WRITE_5M, WRITE_1H = 1.25, 2.0
FAST = 2.0  # speed "fast" is twice the standard rate on every model that has it
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches

def rates(model, prompt_tokens=None) -> tuple:
    """Longest matching prefix; an unknown model pays the highest of each rate."""
    best = ""
    if isinstance(model, str):
        for name in PRICES:
            if (model == name or model.startswith(name + "-")) and len(name) > len(best):
                best = name
    if not best:
        return HIGHEST
    if best == "claude-haiku-5-5" and prompt_tokens is not None and prompt_tokens <= 100_000:
        return HAIKU_55_SHORT
    return PRICES[best]


def _n(v) -> int:
    return v if isinstance(v, int) and v > 0 else 0


def _one(usage: dict, model, fast: bool, long_card: bool = False) -> float:
    inp, out = _n(usage.get("input_tokens")), _n(usage.get("output_tokens"))
    read, wrote = _n(usage.get("cache_read_input_tokens")), _n(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w5, w1 = _n(split.get("ephemeral_5m_input_tokens")), _n(split.get("ephemeral_1h_input_tokens"))
    w1 += max(wrote - w5 - w1, 0)  # writes the usage does not attribute pay the 1-hour rate
    p_in, p_out, p_read = rates(model, None if long_card else inp + read + w5 + w1)
    usd = (inp * p_in + out * p_out + read * p_read + w5 * p_in * WRITE_5M + w1 * p_in * WRITE_1H) / 1e6
    if fast or usage.get("speed") == "fast":
        usd *= FAST
    tools = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    return usd + _n(tools.get("web_search_requests")) * WEB_SEARCH_USD


def cost(usage: dict, models: list, fast: bool = False) -> float:
    """Dollars for one response, at the priciest of `models`. When the usage
    lists per-attempt iterations (fallbacks, advisor, compaction), the sum of
    those counts if it is higher than the top-level figure."""
    top = max(_one(usage, m, fast) for m in models)
    its = usage.get("iterations")
    if not isinstance(its, list):
        return top
    return max(top, sum(max(_one(it, it.get("model") or m, fast) for m in models)
                        for it in its if isinstance(it, dict)))


def request_models(body: dict) -> list:
    """The model a request names, plus any fallback models (None: unknown)."""
    models = [body.get("model")]
    fb = body.get("fallbacks")
    if isinstance(fb, list):
        models += [f.get("model") if isinstance(f, dict) else None for f in fb]
    elif fb is not None:
        models.append(None)
    return models


def is_fast(body: dict) -> bool:
    fb = body.get("fallbacks") if isinstance(body.get("fallbacks"), list) else []
    return body.get("speed") == "fast" or any(isinstance(f, dict) and f.get("speed") == "fast" for f in fb)


def assumed(body: dict, nbytes: int) -> float:
    """What a request is assumed to cost before, or without, a usage report."""
    mt = body.get("max_tokens")
    usage = {"input_tokens": nbytes // 3 + 1, "output_tokens": mt if _n(mt) else MAX_OUTPUT}
    return max(_one(usage, m, is_fast(body), long_card=True) for m in request_models(body))


class Meter:
    """Watches a response go by and picks out the usage and the model."""

    def __init__(self, sse: bool):
        self.sse, self.buf, self.usage, self.model, self.done = sse, b"", {}, None, False

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        if self.sse:
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                if line.startswith(b"data:"):
                    self._event(line[5:])
        elif len(self.buf) > MAX_BODY:
            self.buf, self.sse = b"", True  # too big to parse: give up, charge the assumed amount

    def _event(self, data: bytes) -> None:
        try:
            ev = json.loads(data)
            if ev.get("type") == "message_start":
                self.model = ev["message"].get("model")
                self._merge(ev["message"].get("usage"))
            elif ev.get("type") == "message_delta":
                self._merge(ev.get("usage"))
                self.done = True
        except (ValueError, AttributeError, KeyError, TypeError):
            pass

    def _merge(self, usage) -> None:
        self.usage.update({k: v for k, v in usage.items() if v is not None})

    def final(self) -> dict | None:
        """The complete usage, or None if the response never reported one."""
        if not self.sse:
            self._event(b'{"type":"message_start","message":' + self.buf + b"}")
            self.done = bool(self.usage)
        ok = self.done and all(isinstance(self.usage.get(k), int) for k in ("input_tokens", "output_tokens"))
        return self.usage if ok else None


class Spend:
    """Per-day and per-month totals in one JSON file, plus in-flight reservations."""

    def __init__(self, path: str, daily_cap: float, monthly_cap: float, now=datetime.now):
        self.path, self.now = Path(path), now
        self.caps = {"daily": float(daily_cap), "monthly": float(monthly_cap)}
        self.days, self.months = {}, {}
        self.inflight = 0.0
        self.loaded = self.unsaved = False

    def _keys(self) -> tuple[str, str]:
        t = self.now()
        return t.strftime("%Y-%m-%d"), t.strftime("%Y-%m")

    def _load(self) -> None:
        if not self.path.exists():
            self.loaded = True  # first start
            return
        data = json.loads(self.path.read_text())
        for table in (data["days"], data["months"]):
            for entry in table.values():
                amounts = [entry["total"], *entry["callers"].values()]
                if not all(isinstance(a, (int, float)) and math.isfinite(a) and a >= 0 for a in amounts):
                    raise ValueError("bad amount")
        self.days, self.months, self.loaded = data["days"], data["months"], True

    def _save(self) -> None:
        oldest = (self.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        self.days = {d: v for d, v in self.days.items() if d >= oldest}
        self.unsaved = True
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"days": self.days, "months": self.months}, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
        self.unsaved = False

    def problem(self) -> str | None:
        """None when the state is trustworthy and on disk; otherwise why not."""
        try:
            if not self.loaded:
                self._load()
            if self.unsaved:
                self._save()
        except Exception as e:  # anything wrong here means a human must look
            return f"{type(e).__name__}: {e}"
        return None

    def refusal(self, reserve: float) -> str | None:
        """Why a request needing `reserve` dollars may not start, or None."""
        day, month = self._keys()
        for cap, spent in (("daily", self.days.get(day, {}).get("total", 0.0)),
                           ("monthly", self.months.get(month, {}).get("total", 0.0))):
            limit = self.caps[cap]
            if spent >= limit:
                return f"The relay's {cap} spend cap of ${limit:.2f} is reached (${spent:.2f} spent). No requests are forwarded until it resets or Tom raises it."
            if spent + self.inflight + reserve > limit:
                return (f"The relay's {cap} spend cap of ${limit:.2f} has no room for this request: ${spent:.2f} spent, "
                        f"${self.inflight:.2f} held for requests in flight, and this one could cost up to ${reserve:.2f}.")
        return None

    def charge(self, caller: str, usd: float) -> None:
        for table, key in zip((self.days, self.months), self._keys()):
            entry = table.setdefault(key, {"total": 0.0, "callers": {}})
            entry["total"] = round(entry["total"] + usd, 6)
            entry["callers"][caller] = round(entry["callers"].get(caller, 0.0) + usd, 6)
        try:
            self._save()
        except OSError as e:
            log.error("cannot write %s (%s); refusing requests until it can be written", self.path, type(e).__name__)


SPEND = web.AppKey("spend", Spend)
CLIENT = web.AppKey("client", aiohttp.ClientSession)


def read_key(path: str) -> str:
    key = ""
    for line in Path(path).read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if line.startswith("ANTHROPIC_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
        elif line and not line.startswith("#") and "=" not in line:
            key = line
    if not key:
        raise SystemExit(f"no key found in {path}")
    return key


def caller_of(request: web.Request, tokens_dir: str) -> str | None:
    """The name of the tokens_dir file whose content the request presents."""
    given = request.headers.get("x-api-key", "")
    auth = request.headers.get("Authorization", "")
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    found = None
    try:
        files = sorted(Path(tokens_dir).iterdir())
    except OSError:
        files = []
    for f in files:
        try:
            token = f.read_text().strip() if f.is_file() and not f.name.startswith(".") else ""
        except (OSError, UnicodeDecodeError):
            token = ""
        if token and given and hmac.compare_digest(token.encode(), given.encode()):
            found = f.name
    return found


def error(status: int, kind: str, message: str) -> web.Response:
    # x-should-retry: false stops the SDKs' automatic retries; no retry-after on purpose.
    return web.json_response({"type": "error", "error": {"type": kind, "message": message}},
                             status=status, headers={"x-should-retry": "false"})


def make_app(cfg: dict, now=datetime.now) -> web.Application:
    key = read_key(cfg["key_file"])
    upstream = cfg["upstream"].rstrip("/")
    spend = Spend(cfg["state_path"], cfg["daily_cap_usd"], cfg["monthly_cap_usd"], now)
    if (why := spend.problem()):
        log.error("spend state %s is unusable (%s); refusing all requests", spend.path, why)
    app = web.Application(client_max_size=MAX_BODY)
    app[SPEND] = spend

    async def client(app):
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            app[CLIENT] = session
            yield

    async def handle(request: web.Request) -> web.StreamResponse:
        method, path = request.method, request.path[:200]
        caller = caller_of(request, cfg["tokens_dir"])
        if caller is None:
            log.warning("401 %s %s caller=unknown", method, path)
            return error(401, "authentication_error", "Unknown relay token.")
        if method != "POST" or request.path not in ROUTES:
            log.warning("404 %s %s caller=%s", method, path, caller)
            return error(404, "not_found_error", "The relay forwards only POST /v1/messages and /v1/messages/count_tokens.")
        if (why := spend.problem()):
            log.error("503 %s %s caller=%s: spend state %s is unusable (%s)", method, path, caller, spend.path, why)
            return error(503, "api_error", "The relay cannot trust its spend record and is refusing all requests. Tom needs to look at it.")
        raw = await request.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return error(400, "invalid_request_error", "The request body must be a JSON object.")
        billable = request.path == "/v1/messages"
        reserve = assumed(body, len(raw)) if billable else 0.0
        if (why := spend.refusal(reserve)):
            log.warning("429 %s %s caller=%s: %s", method, path, caller, why)
            return error(429, "rate_limit_error", why)

        # An allowlist, so the caller's token, hop-by-hop headers and anything else stay behind.
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "accept", "user-agent") or k.lower().startswith("anthropic-")}
        headers |= {"x-api-key": key, "Accept-Encoding": "identity"}
        url = upstream + request.path + ("?" + request.query_string if request.query_string else "")
        spend.inflight += reserve
        usd, status, meter = reserve, 0, None  # until proven otherwise, the request costs its reservation
        try:
            async with app[CLIENT].post(url, data=raw, headers=headers, allow_redirects=False) as up:
                status = up.status
                if not (billable and 200 <= status < 300):
                    usd = 0.0
                meter = Meter("text/event-stream" in up.headers.get("Content-Type", ""))
                resp = web.StreamResponse(status=status, headers={
                    k: v for k, v in up.headers.items()
                    if k.lower() in ("content-type", "request-id", "retry-after", "x-should-retry")
                    or k.lower().startswith("anthropic-")})
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    meter.feed(chunk)
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except aiohttp.ClientConnectorError as e:
            usd = 0.0  # never reached upstream
            log.error("502 %s %s caller=%s: %s", method, path, caller, type(e).__name__)
            return error(502, "api_error", "The relay could not reach the upstream API.")
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            log.error("%s %s caller=%s: cut short (%s)", method, path, caller, type(e).__name__)
            if status:
                return resp  # already streaming; nothing more can be said
            return error(502, "api_error", "The upstream API did not answer.")
        finally:
            spend.inflight -= reserve
            if usd:
                usage = meter.final() if meter else None
                if usage:
                    usd = cost(usage, [body.get("model"), meter.model], is_fast(body))
                elif meter and meter.usage:
                    usd = max(usd, cost(meter.usage, [body.get("model"), meter.model], is_fast(body)))
                spend.charge(caller, usd)
            log.info("%s %s %s caller=%s model=%s usd=%.6f%s", status or "-", method, path, caller,
                     str(body.get("model"))[:60], usd, "" if not usd or usage else " (assumed, no usage seen)")

    app.cleanup_ctx.append(client)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = json.loads(Path(os.environ["RELAY_CONFIG"]).read_text())
    web.run_app(make_app(cfg), host=cfg["listen_host"], port=cfg["port"], access_log=None, print=None)


if __name__ == "__main__":
    main()
