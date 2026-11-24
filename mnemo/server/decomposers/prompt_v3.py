"""Decomposer prompt v3 (v2 Phase 2; docs/phase2-design.md §4).

Same rules as v2. Per atom it adds `event_date` and `date_precision`, and
drops `confidence`: confidence now comes from a prior by origin_class
(docs/phase2-design.md §8), not from hedging words. The decomposer still does
not decide the atom's kind.
"""

from __future__ import annotations

from . import prompt_v2
from .prompt_v2 import TOOL_NAME, build_preamble  # noqa: F401  (same preamble)

PROMPT_VERSION = "prompt_v3"

_V2_FIELDS = prompt_v2.RULES[prompt_v2.RULES.index("Fields:"):prompt_v2.RULES.index("Record the atoms with")]
_V3_FIELDS = """Fields:
- text: the atom, one or two sentences.
- type: episodic | semantic | procedural.
- subject: the named person, system or thing the atom is about.
- source_quote: as rule 9.
- event_date: YYYY-MM-DD the atom's time refers to: when the event happened or the reading was taken; for a plan, the planned date; for a state, the date it is stated to hold from (the note's date for "As of …"). If only the month is known, use the 1st of that month; if only the year, 1 January of that year. null if the note gives no date for it.
- date_precision: how precisely the note gives event_date — day, month or year; unknown when event_date is null. Never claim day when the note gives only a month or a year.
- entity_resolved: false only under rule 6.

"""
assert _V2_FIELDS in prompt_v2.RULES
RULES = prompt_v2.RULES.replace(_V2_FIELDS, _V3_FIELDS)

RECORD_ATOMS_SCHEMA = {
    "type": "object",
    "required": ["atoms"],
    "properties": {
        "atoms": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "type", "subject", "source_quote", "event_date", "date_precision"],
                "properties": {
                    "text": {"type": "string"},
                    "type": {"enum": ["episodic", "semantic", "procedural"]},
                    "subject": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "event_date": {"type": ["string", "null"]},
                    "date_precision": {"enum": ["day", "month", "year", "unknown"]},
                    "entity_resolved": {"type": "boolean"},
                },
            },
        },
    },
}

RECORD_ATOMS_TOOL = {
    "name": TOOL_NAME,
    "description": "Record the memory atoms extracted from the note.",
    "input_schema": RECORD_ATOMS_SCHEMA,
}


def build_system_prompt(req) -> str:
    return f"{build_preamble(req)}\n\n{RULES}"
