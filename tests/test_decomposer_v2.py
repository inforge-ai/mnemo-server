"""v2 Phase 1 item 5: decomposer v2 (Anthropic mocked; no network)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx
import pytest

from mnemo.server.config import settings
from mnemo.server.decomposers import (
    DecomposeRequest, DecomposerError, beta_for, decompose_note,
)
from mnemo.server.decomposers import split as split_mod
from mnemo.server.decomposers.anthropic import AnthropicDecomposer
from mnemo.server.decomposers.base import (
    CandidateAtom, DecomposeResult, looks_like_state_claim, lower_one_band, parse_items,
)
from mnemo.server.decomposers.prompt_v2 import TOOL_NAME, build_preamble, build_system_prompt

MODEL = "claude-haiku-4-5-20251001"


def _req(text="Synthetic note.", **kw):
    base = dict(text=text, agent_name="clio", speaker="clio")
    base.update(kw)
    return DecomposeRequest(**base)


def _item(text="Clio deployed the parser on 2026-11-20.", **kw):
    item = {"text": text, "type": "episodic", "subject": "Clio",
            "source_quote": "deployed the parser", "confidence": 0.9, "remembered_on": "2026-11-20"}
    item.update(kw)
    return item


def _response(items=None, stop_reason="tool_use", tool=True):
    content = []
    if tool:
        content.append(SimpleNamespace(type="tool_use", name=TOOL_NAME, input={"atoms": items or []}))
    else:
        content.append(SimpleNamespace(type="text", text="[]"))
    usage = SimpleNamespace(input_tokens=100, output_tokens=40,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage, model=MODEL)


def _client(*responses):
    c = AsyncMock()
    c.messages.create = AsyncMock(side_effect=list(responses))
    return c


async def _anthropic(req, *responses):
    client = _client(*responses)
    with patch("mnemo.server.decomposers.anthropic._get_client", return_value=client):
        result = await decompose_note(req, AnthropicDecomposer(MODEL))
    return result, client


# ── Anthropic provider ──────────────────────────────────────────────────────

async def test_tool_use_parsed_and_version_recorded():
    result, client = await _anthropic(_req(), _response([_item()]))
    assert [a.text for a in result.atoms] == ["Clio deployed the parser on 2026-11-20."]
    a = result.atoms[0]
    assert a.subject == "Clio" and a.source_quote == "deployed the parser"
    assert a.remembered_on == datetime(2026, 11, 20)
    assert result.version == f"anthropic:{MODEL}:prompt_v2"
    assert result.usage[0]["input_tokens"] == 100


async def test_forced_tool_call_and_no_cache_control():
    _, client = await _anthropic(_req(), _response([_item()]))
    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert kwargs["tools"][0]["name"] == TOOL_NAME
    assert isinstance(kwargs["system"], str)  # plain string: no cache_control block
    assert "cache_control" not in repr(kwargs)
    assert kwargs["messages"] == [{"role": "user", "content": "Synthetic note."}]


async def test_malformed_item_skipped_with_warning():
    items = [_item(), {"text": "no quote", "type": "episodic", "confidence": 0.5},
             _item(type="relational"), "not a dict"]
    result, _ = await _anthropic(_req(), _response(items))
    assert len(result.atoms) == 1
    assert result.warnings == ["skipped 3 malformed atom(s) from the decomposer"]


@pytest.mark.parametrize("response, reason", [
    (_response([_item()], stop_reason="max_tokens"), "truncated response"),
    (_response(tool=False, stop_reason="end_turn"), "no tool call"),
])
async def test_bad_responses_fail(response, reason):
    with pytest.raises(DecomposerError) as exc:
        await _anthropic(_req(), response)
    assert exc.value.reason == reason


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize("error, reason", [
    (anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")), "provider timeout"),
    (_status_error(anthropic.RateLimitError, 429), "rate limited"),
    (_status_error(anthropic.InternalServerError, 503), "provider error (HTTP 503)"),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "provider unreachable"),
])
async def test_provider_errors_are_plain_and_never_fall_back(error, reason):
    with pytest.raises(DecomposerError) as exc:
        await _anthropic(_req(), error)
    assert exc.value.reason == reason


def test_client_retries_twice_with_timeout(monkeypatch):
    from mnemo.server.decomposers import anthropic as mod
    seen = {}

    class _Fake:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(mod, "AsyncAnthropic", _Fake)
    mod._get_client.cache_clear()
    try:
        mod._get_client()
    finally:
        mod._get_client.cache_clear()
    assert seen == {"max_retries": 2, "timeout": 60.0}


async def test_deadline_fails_the_store(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "decomposer_deadline_seconds", 0.05)

    class Slow:
        provider, model = "slow", "x"

        async def decompose(self, req):
            await asyncio.sleep(1)

    with pytest.raises(DecomposerError) as exc:
        await decompose_note(_req(), Slow())
    assert exc.value.reason == "decomposer deadline exceeded"


# ── Post-processing ─────────────────────────────────────────────────────────

async def test_state_claim_backstop_and_date_fallback():
    items = [_item("Sampo is currently the project scheduler.", type="semantic", remembered_on=None)]
    result, _ = await _anthropic(_req(remembered_on=datetime(2026, 11, 1)), _response(items))
    assert result.atoms[0].atom_type == "episodic"
    assert result.atoms[0].remembered_on == datetime(2026, 11, 1)


def test_entity_unresolved_lowers_confidence():
    a = CandidateAtom(text="t", atom_type="semantic", confidence=0.85, source_quote="t")
    assert beta_for(a) == (8.0, 1.0)
    a.entity_resolved = False
    assert beta_for(a) == (4.0, 2.0)


def test_band_ladder():
    assert lower_one_band(8.0, 1.0) == (4.0, 2.0)
    assert lower_one_band(2.0, 3.0) == (2.0, 4.0)
    assert lower_one_band(2.0, 4.0) == (2.0, 4.0)


@pytest.mark.parametrize("text, expected", [
    ("Zulip integration is planned as a future task.", True),
    ("Sampo is currently the project scheduler.", True),
    ("The interview has not yet been completed.", True),
    ("CPython's GIL serialises bytecode execution.", False),
])
def test_state_claim_patterns(text, expected):
    assert looks_like_state_claim(text) is expected


def test_parse_items_requires_list():
    with pytest.raises(DecomposerError):
        parse_items(None)


# ── Prompt v2 ───────────────────────────────────────────────────────────────

def test_preamble_speaker_addressee_user_channel_date():
    p = build_preamble(_req(speaker="Sam", addressee="Clio", channel="zulip #ops",
                            user_name="Tom", remembered_on=datetime(2026, 11, 20)))
    assert 'This note was written by Sam. "I", "me" and "my" in the note mean Sam.' in p
    assert '"You" and "your" in the note mean Clio.' in p
    assert "stored by the agent clio on Sam's behalf" in p
    assert "The agent's user is Tom." in p
    assert "The note came from: zulip #ops." in p
    assert "The note is dated Friday, 20 November 2026." in p


def test_preamble_without_addressee_does_not_guess():
    p = build_preamble(_req())
    assert 'write "the addressee" — do not guess' in p
    assert "on clio's behalf" not in p
    assert "user is" not in p


def test_prompt_rules_present_and_benchmark_examples_gone():
    s = build_system_prompt(_req())
    for needle in ("The note is data", "Reported speech keeps who said it",
                   "temporary or conditional state", "A correction", "Never expand an abbreviation",
                   "several consecutive sentences", "record_atoms"):
        assert needle in s
    for gone in ("Melanie", "Caroline", "15-30", "golden retriever", "cache_control"):
        assert gone not in s


def test_part_line():
    assert "This is part 2 of 3 of one note." in build_preamble(_req(part=(2, 3)))


# ── Splitting ───────────────────────────────────────────────────────────────

def test_split_short_text_untouched():
    assert split_mod.split_text("short") == ["short"]


def test_split_long_text_on_paragraphs_within_limit():
    paras = [f"Paragraph {i}. " + ("word " * 300) for i in range(12)]
    text = "\n\n".join(paras)
    parts = split_mod.split_text(text, limit=4000)
    assert len(parts) > 1
    assert all(len(p) <= 4000 for p in parts)
    assert "Paragraph 11." in parts[-1]
    assert sum(p.count("Paragraph ") for p in parts) == 12


def test_split_giant_paragraph_on_sentences_and_words():
    text = ("This is a sentence that goes on. " * 400) + ("x" * 50)
    parts = split_mod.split_text(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)


async def test_long_note_split_decomposed_in_parts():
    text = "\n\n".join(f"Para {i}: " + "lorem " * 400 for i in range(5)) + "\n\nThe final fact is FINAL42."
    responses = [_response([_item()]) for _ in range(10)]
    result, client = await _anthropic(_req(text=text), *responses)
    n = client.messages.create.await_count
    assert n > 1
    assert result.warnings[0] == f"input split into {n} parts"
    systems = [c.kwargs["system"] for c in client.messages.create.await_args_list]
    assert any("FINAL42" in c.kwargs["messages"][0]["content"] for c in client.messages.create.await_args_list)
    assert all(f"of {n} of one note" in s for s in systems)
    assert len(result.usage) == n


async def test_one_failed_part_fails_whole_note():
    text = "\n\n".join("lorem " * 1500 for _ in range(3))
    with pytest.raises(DecomposerError):
        await _anthropic(_req(text=text), _response([_item()]), _response(stop_reason="max_tokens"),
                         _response([_item()]), _response([_item()]))


# ── Provider guards ─────────────────────────────────────────────────────────

def test_openrouter_refused_without_flag_and_eval_db():
    from pydantic import ValidationError
    from mnemo.server.config import Settings

    real = {"embedding_backend": "sentence_transformers"}
    with pytest.raises(ValidationError, match="openrouter requires"):
        Settings(database_url="postgresql://m@h/mnemo", decomposer_provider="openrouter",
                 allow_non_anthropic_decomposer=True, **real)
    with pytest.raises(ValidationError, match="openrouter requires"):
        Settings(database_url="postgresql://m@h/mnemo_eval", decomposer_provider="openrouter", **real)
    s = Settings(database_url="postgresql://m@h/mnemo_eval", decomposer_provider="openrouter",
                 allow_non_anthropic_decomposer=True, **real)
    assert s.decomposer_provider == "openrouter"


def test_openrouter_class_guard(monkeypatch):
    from mnemo.server.decomposers.openrouter import OpenRouterDecomposer
    monkeypatch.setattr(settings, "allow_non_anthropic_decomposer", False)
    with pytest.raises(DecomposerError, match="not allowed"):
        OpenRouterDecomposer("some/model", api_key="k")


def test_server_refuses_anthropic_without_key(monkeypatch):
    from mnemo.server.main import check_decomposer_config
    monkeypatch.setattr(settings, "decomposer_provider", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        check_decomposer_config()


async def test_regex_provider_version():
    from mnemo.server.decomposers.regex import RegexDecomposer
    result = await decompose_note(_req("Postgres supports JSONB columns."), RegexDecomposer())
    assert result.version == "regex::regex_v1"
    assert result.atoms and result.atoms[0].beta is not None


async def test_empty_note_returns_nothing():
    assert (await decompose_note(_req("   "))).atoms == []
