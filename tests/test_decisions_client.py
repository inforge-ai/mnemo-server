"""v2 Phase 2: OpenRouter Decisions client (mock transport only; no network)."""

import json
import time

import httpx
import pytest

from mnemo.server.decisions import choice, noul
from mnemo.server.decisions import openrouter as orc
from mnemo.server.decisions.fake import FakeDecisions

Q = {"relation": choice("How does NEW relate to EXISTING?", {"same": "s", "unrelated": "u"}),
     "change": noul("Did it change?", "yes", "no")}
OK = {"id": "x", "model": "typesafe/jev-1.14-20261014",
      "answers": {"relation": {"type": "choice", "choice": "same", "probabilities": {"same": 0.9, "unrelated": 0.1}},
                  "change": {"type": "noul", "noul": 0.2}},
      "usage": {"input_tokens": 40, "output_tokens": 0, "cost": 0.00002}}


class Recorder:
    """MockTransport handler: replies from a list, records every request body."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(rec, **kw):
    return orc.OpenRouterDecisions(model="typesafe/jev-1.14", fallback_model="liquid/d1.2-ultra",
                                   api_key="test-key", transport=httpx.MockTransport(rec), **kw)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    slept = []

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr(orc.asyncio, "sleep", sleep)
    return slept


def assert_all_zdr(rec):
    assert rec.bodies, "no request was sent"
    for b in rec.bodies:
        assert b["provider"] == {"zdr": True}


async def test_success_parses_answers_and_sends_zdr_only():
    rec = Recorder([httpx.Response(200, json=OK)])
    res = await client(rec).ask("STATE", Q)
    assert res.ok and res.model == "typesafe/jev-1.14-20261014" and res.cost == 0.00002 and res.attempts == 1
    assert res.probabilities("relation") == {"same": 0.9, "unrelated": 0.1} and res.p_true("change") == 0.2
    body = rec.bodies[0]
    assert set(body) == {"model", "state", "questions", "provider"}
    assert body["provider"] == {"zdr": True} and body["model"] == "typesafe/jev-1.14" and body["questions"] == Q


async def test_retry_on_429_honours_retry_after(_fast_sleep):
    rec = Recorder([httpx.Response(429, headers={"Retry-After": "0.7"}), httpx.Response(200, json=OK)])
    res = await client(rec).ask("S", Q)
    assert res.ok and res.attempts == 2 and _fast_sleep == [0.7]
    assert_all_zdr(rec)


async def test_two_5xx_retries_then_fallback_model():
    rec = Recorder([httpx.Response(502), httpx.Response(503), httpx.Response(529), httpx.Response(200, json=OK)])
    res = await client(rec).ask("S", Q)
    assert res.ok and res.attempts == 4
    assert [b["model"] for b in rec.bodies] == ["typesafe/jev-1.14"] * 3 + ["liquid/d1.2-ultra"]
    assert_all_zdr(rec)


async def test_non_retryable_goes_straight_to_fallback():
    rec = Recorder([httpx.Response(400, json={"error": {"code": 400}}), httpx.Response(200, json=OK)])
    res = await client(rec).ask("S", Q)
    assert res.ok and [b["model"] for b in rec.bodies] == ["typesafe/jev-1.14", "liquid/d1.2-ultra"]


@pytest.mark.parametrize("limit_source, retried", [("openrouter_in_flight_budget", True), ("openrouter_credits", False)])
async def test_402_only_in_flight_budget_is_transient(limit_source, retried):
    r402 = httpx.Response(402, json={"error": {"code": 402, "metadata": {"limit_source": limit_source}}})
    rec = Recorder([r402, httpx.Response(200, json=OK), httpx.Response(200, json=OK)])
    await client(rec).ask("S", Q)
    assert rec.bodies[1]["model"] == ("typesafe/jev-1.14" if retried else "liquid/d1.2-ultra")


async def test_network_errors_retry_then_give_up_without_raising():
    rec = Recorder([httpx.ConnectError("boom")] * 6)
    res = await client(rec).ask("S", Q)
    assert not res.ok and res.attempts == 6 and "ConnectError" in res.error
    assert_all_zdr(rec)


async def test_deadline_stops_new_attempts():
    rec = Recorder([httpx.Response(503)] * 6)
    res = await client(rec).ask("S", Q, deadline=time.monotonic() - 1)
    assert not res.ok and res.error == "deadline" and rec.bodies == []


async def test_no_key_sends_nothing(monkeypatch):
    monkeypatch.setattr(orc.settings, "openrouter_api_key", None)
    rec = Recorder([httpx.Response(200, json=OK)])
    c = orc.OpenRouterDecisions(transport=httpx.MockTransport(rec), api_key=None)
    res = await c.ask("S", Q)
    assert not res.ok and res.error == "no OpenRouter key" and rec.bodies == []


async def test_body_without_zdr_is_never_sent(monkeypatch):
    """The ZDR proof: even if body construction were broken, nothing goes out."""
    monkeypatch.setattr(orc, "build_body", lambda m, s, q: {"model": m, "state": s, "questions": q})
    rec = Recorder([httpx.Response(200, json=OK)])
    with pytest.raises(orc.ZdrViolation):
        await client(rec).ask("S", Q)
    assert rec.bodies == []


@pytest.mark.parametrize("provider", [{"zdr": False}, {"zdr": True, "allow_fallbacks": True}, {}])
async def test_check_zdr_rejects_anything_but_exactly_zdr_true(provider):
    with pytest.raises(orc.ZdrViolation):
        orc._check_zdr({"model": "m", "state": "s", "questions": {}, "provider": provider})


def test_build_body_always_zdr():
    assert orc.build_body("m", "s", {})["provider"] == {"zdr": True}


def test_real_network_client_refused_in_tests():
    with pytest.raises(RuntimeError, match="non-eval test"):
        orc.OpenRouterDecisions(api_key="k")


async def test_fake_outage_fails_open():
    def boom(state, questions):
        raise RuntimeError("outage")
    res = await FakeDecisions(boom).ask("S", Q)
    assert not res.ok and "outage" in res.error


def test_fake_provider_needs_test_db():
    from mnemo.server.config import Settings
    with pytest.raises(ValueError, match="DECISION_PROVIDER=fake"):
        Settings(decision_provider="fake", database_url="postgresql://x@h/mnemo")
