"""
Breaks free-text input into typed memory atoms with inferred confidence.

v0.1 IMPLEMENTATION: Rule-based classifier.

CLASSIFICATION RULES:

Episodic markers:
  - Past tense first-person: "I found", "I discovered", "I encountered", etc.
  - Temporal references: "today", "yesterday", "just now", "while working on"
  - Specific context: file names, row numbers, error messages, timestamps

Procedural markers:
  - Imperative mood: "always", "never", "should", "must", "make sure"
  - Action verbs: "use", "avoid", "prefer" with "instead", "rather", etc.
  - Pattern: "when X, do Y", "to prevent X, do Y"

Semantic (default):
  - General statements of fact
  - Descriptions of how things work
  - Observations without personal context

CONFIDENCE INFERENCE:

High confidence — Beta(8, 1):
  - Episodic atoms (direct observation)
  - Phrases: "confirmed", "verified", "tested", "definitely"

Moderate confidence — Beta(4, 2):
  - Inferred facts and procedures (default)

Low confidence — Beta(2, 3):
  - Hedging language: "I think", "maybe", "possibly", "might"

Very low confidence — Beta(2, 4):
  - Strong uncertainty: "I don't know if", "it could be"
"""

import re
from dataclasses import dataclass, field


@dataclass
class DecomposedAtom:
    text: str
    atom_type: str              # episodic, semantic, procedural
    confidence_alpha: float
    confidence_beta: float
    structured: dict = field(default_factory=dict)


# Marker patterns
EPISODIC_PATTERNS = [
    r'\bI\s+(found|discovered|encountered|noticed|observed|hit|ran into|saw|tried|learned|realized|realised)\b',
    r'\b(today|yesterday|just now|this morning|last night|earlier)\b',
    r'\b(while|when I was)\s+(working|processing|debugging|testing|deploying|running|building)\b',
    r'\bI\s+(was|have been|had)\s+(working|debugging|testing|running)\b',
]

PROCEDURAL_PATTERNS = [
    r'\b(always|never|should|must|make sure|be sure to|remember to)\b',
    r'\b(use|avoid|prefer|check|validate|specify|ensure)\b.{0,60}\b(instead|rather|before|after|otherwise)\b',
    r'\b(when|if)\b.{0,60}\b(do|use|try|run|set|add|make sure)\b',
    r'\b(to prevent|to avoid|to fix|to handle|in order to)\b',
    r'\b(best practice|pro tip|rule of thumb|lesson learned)\b',
    r'\b(don\'t|do not)\s+(use|forget|ignore|skip)\b',
]

HIGH_CONFIDENCE_PATTERNS = [
    r'\b(confirmed|verified|tested|definitely|certainly|proven|always works|guaranteed)\b',
]

LOW_CONFIDENCE_PATTERNS = [
    r'\b(I think|maybe|possibly|might|perhaps|seems? like|appears? to|I believe|I suspect)\b',
    r'\b(not sure|unclear|uncertain|don\'t know|unsure|it seems)\b',
]

VERY_LOW_CONFIDENCE_PATTERNS = [
    r'\b(I don\'t know if|it could be|might be wrong|not certain|could be wrong)\b',
]


def decompose(text: str, domain_tags: list[str] | None = None) -> list[DecomposedAtom]:
    """Break free-text into typed atoms with inferred confidence."""
    sentences = _split_sentences(text)
    atoms = []

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 10:
            continue

        atom_type = _classify_type(sentence)
        alpha, beta = _infer_confidence(sentence, atom_type)
        structured = _extract_structured(sentence)

        atoms.append(DecomposedAtom(
            text=sentence,
            atom_type=atom_type,
            confidence_alpha=alpha,
            confidence_beta=beta,
            structured=structured,
        ))

    return _merge_adjacent(atoms)


def infer_edges(atoms: list[DecomposedAtom]) -> list[tuple[int, int, str]]:
    """
    Return (source_idx, target_idx, edge_type) triples between atoms from the
    same /remember call, following the spec rules:

      - episodic  --evidence_for-->  semantic
      - procedural --motivated_by--> semantic
      - episodic  --evidence_for-->  procedural (if no semantic present)
    """
    edges = []
    episodic_idxs = [i for i, a in enumerate(atoms) if a.atom_type == "episodic"]
    semantic_idxs = [i for i, a in enumerate(atoms) if a.atom_type == "semantic"]
    procedural_idxs = [i for i, a in enumerate(atoms) if a.atom_type == "procedural"]

    for e_idx in episodic_idxs:
        for s_idx in semantic_idxs:
            edges.append((e_idx, s_idx, "evidence_for"))
        if not semantic_idxs:
            for p_idx in procedural_idxs:
                edges.append((e_idx, p_idx, "evidence_for"))

    for p_idx in procedural_idxs:
        for s_idx in semantic_idxs:
            edges.append((p_idx, s_idx, "motivated_by"))

    return edges


def _split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, keeping code blocks and dotted identifiers intact."""
    # Protect inline code blocks from being split mid-sentence
    protected = re.sub(r'`[^`]+`', lambda m: m.group().replace('.', '\x00'), text)
    # Protect dotted identifiers (e.g. pd.read_csv, torch.nn.Module, os.path.join)
    protected = re.sub(
        r'(\b[a-z_]\w*)\.(\w)',
        lambda m: m.group(1) + '\x00' + m.group(2),
        protected,
    )
    parts = re.split(r'(?<=[.!?])\s+', protected)
    return [p.replace('\x00', '.') for p in parts]


def _classify_type(sentence: str) -> str:
    # Check procedural FIRST — imperative markers are a stronger signal than
    # first-person voice (e.g. "I will always use X" is procedural, not episodic)
    for pattern in PROCEDURAL_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return "procedural"
    for pattern in EPISODIC_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return "episodic"
    return "semantic"


def _infer_confidence(sentence: str, atom_type: str) -> tuple[float, float]:
    for pattern in VERY_LOW_CONFIDENCE_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return (2.0, 4.0)
    for pattern in LOW_CONFIDENCE_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return (2.0, 3.0)
    for pattern in HIGH_CONFIDENCE_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return (8.0, 1.0)

    if atom_type == "episodic":
        return (8.0, 1.0)
    return (4.0, 2.0)


def _extract_structured(sentence: str) -> dict:
    """Extract inline code snippets."""
    code_match = re.search(r'`([^`]+)`', sentence)
    if code_match:
        return {"code": code_match.group(1)}
    return {}


def _merge_adjacent(atoms: list[DecomposedAtom]) -> list[DecomposedAtom]:
    """Merge adjacent atoms of the same type into a single atom."""
    if not atoms:
        return []
    merged = [atoms[0]]
    for atom in atoms[1:]:
        prev = merged[-1]
        if atom.atom_type == prev.atom_type:
            merged[-1] = DecomposedAtom(
                text=prev.text + " " + atom.text,
                atom_type=prev.atom_type,
                confidence_alpha=max(prev.confidence_alpha, atom.confidence_alpha),
                confidence_beta=min(prev.confidence_beta, atom.confidence_beta),
                structured={**prev.structured, **atom.structured},
            )
        else:
            merged.append(atom)
    return merged
