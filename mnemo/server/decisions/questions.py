"""Decision-model questions and state templates (docs/phase2-design.md §2).

The wording is fixed and versioned: QUESTIONS_VERSION is recorded with every
decision. Change it only with a new version and a re-run of the bake-off.
"""

from __future__ import annotations

from datetime import datetime

from .base import choice, noul

QUESTIONS_VERSION = "q2"   # q2 (Tom, 2026-11-24): `same` criterion reworded

KIND_INSTRUCTIONS = ("What kind of statement is the ATOM? Judge the ATOM itself. "
                     "The CONTEXT only helps with names and dates.")
KIND_CRITERIA = {
    "state": ("A property or situation that holds from some time until it changes: a status, a current "
              "value or setting, a baseline, where something is, what something uses."),
    "plan": "An intention, booking, schedule or expectation about something that has not happened yet.",
    "observation": ("Something that happened or was measured at a particular time: an event, an action "
                    "taken, a single reading."),
    "rule": "A general fact, preference, procedure or definition that is not tied to a particular time.",
}
KINDS = tuple(KIND_CRITERIA)

GROUNDED = noul(
    "Is the ATOM fully supported by the QUOTE? Answer true only if every claim in the ATOM (numbers, names, "
    "abbreviations, who did what, negation) is stated or directly implied by the QUOTE. The CONTEXT says who "
    "wrote the quote and when; the ATOM may use it for names and dates.",
    "The quote fully supports the atom.",
    "The atom adds, changes or contradicts something in the quote.",
)
TIME_UNRESOLVED = noul(
    "Does the ATOM refer to a time relative to a date it does not state (for example 'tomorrow', 'last week', "
    "'on Monday'), so that the actual date cannot be worked out from the ATOM, its EVENT DATE and the note "
    "date? Words such as 'now' or 'currently' that only say something is presently true do not count.",
    "The atom depends on a reference date it does not give.",
    "The atom's time is stated or can be worked out, or the words only mark a current state.",
)

RELATION_INSTRUCTIONS = "How does the NEW ATOM relate to the EXISTING ATOM?"
RELATION_CRITERIA = {
    "same": ("Both state the same fact about the same thing. A state or rule restated later with the same "
             "value counts as the same. Two observations are the same only if they describe the same single "
             "event."),
    "replaces": ("The new atom makes the existing one out of date: a newer value of the same property, a "
                 "changed state, a plan that happened, was cancelled or was moved, or an explicit correction. "
                 "The existing atom was true or believed earlier and is no longer current."),
    "refines": "The new atom qualifies, scopes or adds detail to the existing one without making it false.",
    "conflict": ("The atoms make incompatible claims about the same thing for the same time, and neither "
                 "says it corrects the other."),
    "unrelated": ("They mention overlapping people, systems or topics but state independent facts. Two "
                  "readings or events on different days are independent of each other."),
}
CHANGE = noul(
    "Does the NEW ATOM report that the situation in the EXISTING ATOM has changed (the state ended or took a "
    "new value), or that the planned thing happened, was cancelled or was moved? A reading of a different "
    "quantity, or another event of the same kind, is not a change.",
    "It reports that change.", "It does not.",
)
SAME_ATTRIBUTE = noul(
    "Are both atoms about the same property of the same subject (for example both give one person's resting "
    "heart-rate baseline, or both say which database one service uses)?",
    "Same subject, same property.", "A different subject or a different property.",
)
CORRECTION = noul(
    "Do both atoms describe the same single event or measurement, with the NEW ATOM correcting a detail of it?",
    "Same event; the new atom corrects it.", "Different events, or no correction.",
)


def kind_question() -> dict:
    return choice(KIND_INSTRUCTIONS, KIND_CRITERIA)


def atom_questions(time_prefilter: bool) -> dict[str, dict]:
    qs = {"kind": kind_question(), "grounded": GROUNDED}
    if time_prefilter:
        qs["time_unresolved"] = TIME_UNRESOLVED
    return qs


def pair_questions() -> dict[str, dict]:
    """All four, on every pair (G1 change 6)."""
    return {"relation": choice(RELATION_INSTRUCTIONS, RELATION_CRITERIA), "change": CHANGE,
            "same_attribute": SAME_ATTRIBUTE, "correction": CORRECTION}


def _d(v: datetime | None) -> str | None:
    return v.strftime("%Y-%m-%d") if v else None


def atom_state(atom_text: str, quote: str | None, speaker: str | None, addressee: str | None,
               remembered_on: datetime | None, created_at: datetime | None,
               event_date: datetime | None, date_precision: str) -> str:
    who = f"written by {speaker or 'unknown'}" + (f" to {addressee}" if addressee else "")
    return (f"CONTEXT: {who}; note dated {_d(remembered_on) or 'undated'}; stored {_d(created_at) or 'unknown'}.\n"
            f"QUOTE:\n{quote or atom_text}\n"
            f"ATOM:\n{atom_text}\n"
            f"EVENT DATE: {_d(event_date) or 'none'} (precision: {date_precision})")


def _side(label: str, text: str, kind: str, valid_from: datetime | None, precision: str, origin: str) -> str:
    return (f"{label} (kind {kind}; event date {_d(valid_from) or 'none'}, precision {precision}; "
            f"origin {origin}):\n{text}")


def pair_state(old: dict, new: dict) -> str:
    """old/new: text, kind, valid_from, precision, origin_class."""
    return (_side("EXISTING ATOM", old["text"], old["kind"], old.get("valid_from"), old["precision"], old["origin_class"])
            + "\n\n"
            + _side("NEW ATOM", new["text"], new["kind"], new.get("valid_from"), new["precision"], new["origin_class"]))
