"""Client for the local Ollama server, in two supporting roles.

embed() turns text into a vector; decide()/route() ask the small decision
model a multiple-choice question. Every method fails soft: any error,
timeout, or an unset URL returns None, because the service must keep working
when the machine hosting Ollama is down.
"""

from __future__ import annotations

import logging

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

EMBED_CHARS = 4000
STATE_CHARS = 3000  # the decision model's context is about 2,000 tokens


class Local:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession | None = None):
        self.cfg = cfg
        self._session = session
        self._owns_session = session is None

    async def _post(self, path: str, body: dict, timeout: float) -> dict | None:
        base = self.cfg.ollama_url.rstrip("/")
        if not base:
            return None
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            async with self._session.post(
                base + path, json=body, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    log.warning("ollama %s -> HTTP %s", path, resp.status)
                    return None
                data = await resp.json()
                return data if isinstance(data, dict) else None
        except Exception as e:  # timeouts, refused connections, bad JSON: all soft
            log.warning("ollama %s failed: %r", path, e)
            return None

    async def embed(self, text: str) -> list[float] | None:
        body = {"model": self.cfg.embed_model, "input": text[:EMBED_CHARS]}
        data = await self._post("/api/embed", body, timeout=5)
        try:
            vec = data["embeddings"][0] if data else None
            return [float(x) for x in vec] if vec else None
        except Exception:
            return None

    async def decide(self, state: str | dict, questions: dict) -> dict | None:
        if not self.cfg.decision_model:
            return None
        if isinstance(state, str):
            state = state[:STATE_CHARS]
        body = {"model": self.cfg.decision_model, "state": state, "questions": questions}
        data = await self._post("/v1/systemone", body, timeout=10)
        answers = data.get("answers") if data else None
        return answers if isinstance(answers, dict) else None

    async def route(
        self, text: str, members: dict[str, str], min_confidence: float = 0.6
    ) -> str | None:
        criteria = {**members, "none": "none of these"}
        questions = {
            "owner": {
                "type": "choice",
                "instructions": "Which member should own this request?",
                "criteria": criteria,
            }
        }
        answers = await self.decide(text, questions)
        try:
            ans = answers["owner"] if answers else None
            choice = ans["choice"]
            if choice == "none" or choice not in members:
                return None
            return choice if float(ans["confidence"]) >= min_confidence else None
        except Exception:
            return None

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None
