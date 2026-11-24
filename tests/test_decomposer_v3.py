"""v2 Phase 2: decomposer prompt v3 (event_date, date_precision, no confidence)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mnemo.server.config import settings
from mnemo.server.decomposers import DecomposeRequest, decompose_note
from mnemo.server.decomposers import prompt_v2, prompt_v3
from mnemo.server.decomposers.anthropic import AnthropicDecomposer
from mnemo.server.decomposers.base import parse_items, prompt_module

MODEL = "claude-haiku-5-5"


def _item(**kw):
    item = {"text": "Clio deployed the parser on 2026-11-20.", "type": "episodic", "subject": "Clio",
            "source_quote": "deployed the parser", "event_date": "2026-11-20", "date_precision": "day"}
    item.update(kw)
    return item


def test_v3_is_the_default_and_v2_selectable(monkeypatch):
    assert settings.decomposer_prompt == "v3" and prompt_module() is prompt_v3
    monkeypatch.setattr(settings, "decomposer_prompt", "v2")
    assert prompt_module() is prompt_v2


def test_v3_rules_keep_v2_rules_and_swap_fields():
    assert prompt_v3.RULES.split("Fields:")[0] == prompt_v2.RULES.split("Fields:")[0]
    fields = prompt_v3.RULES.split("Fields:")[1]
    assert "event_date" in fields and "date_precision" in fields
    assert "confidence" not in fields and "remembered_on" not in fields
    props = prompt_v3.RECORD_ATOMS_SCHEMA["properties"]["atoms"]["items"]
    assert "confidence" not in props["properties"] and {"event_date", "date_precision"} <= set(props["required"])


@pytest.mark.parametrize("event_date, precision, want_date, want_precision", [
    ("2026-11-20", "day", datetime(2026, 11, 20), "day"),
    ("2026-08-01", "month", datetime(2026, 8, 1), "month"),
    ("2026-01-01", "year", datetime(2026, 1, 1), "year"),
    (None, "day", None, "unknown"),                 # no date: precision forced to unknown
    ("2026-11-20", "unknown", datetime(2026, 11, 20), "day"),   # a date with no usable precision: day
    ("not a date", "day", None, "unknown"),
])
def test_parse_v3_dates(event_date, precision, want_date, want_precision):
    atoms, warnings = parse_items([_item(event_date=event_date, date_precision=precision)])
    a = atoms[0]
    assert (a.event_date, a.date_precision, a.remembered_on) == (want_date, want_precision, want_date)
    assert a.confidence_from_prior and not warnings


def test_parse_v2_items_unchanged():
    atoms, _ = parse_items([{"text": "t", "type": "semantic", "subject": "s", "source_quote": "t",
                             "confidence": 0.8, "remembered_on": "2026-11-20"}])
    a = atoms[0]
    assert a.confidence == 0.8 and not a.confidence_from_prior
    assert a.remembered_on == datetime(2026, 11, 20) and a.event_date is None and a.date_precision == "unknown"


async def test_anthropic_sends_v3_tool_and_records_version():
    content = [SimpleNamespace(type="tool_use", name="record_atoms", input={"atoms": [_item()]})]
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=SimpleNamespace(stop_reason="tool_use", content=content,
                                                                    usage=usage, model=MODEL))
    req = DecomposeRequest(text="Synthetic note.", agent_name="clio", speaker="clio")
    with patch("mnemo.server.decomposers.anthropic._get_client", return_value=client):
        result = await decompose_note(req, AnthropicDecomposer(MODEL))
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["tools"] == [prompt_v3.RECORD_ATOMS_TOOL]
    assert "event_date" in kwargs["system"]
    assert result.version == f"anthropic:{MODEL}:prompt_v3"
    assert result.atoms[0].date_precision == "day"
