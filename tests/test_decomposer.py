"""
Unit tests for the rule-based decomposer.
No database or async required.
"""

import pytest
from mnemo.server.decomposer import decompose, infer_edges, DecomposedAtom


# ── Type classification ───────────────────────────────────────────────────────

class TestTypeClassification:
    def test_episodic_first_person_past(self):
        atoms = decompose("I found a bug in the auth module.")
        assert atoms[0].atom_type == "episodic"

    def test_episodic_discovered(self):
        atoms = decompose("I discovered the issue while debugging production.")
        assert atoms[0].atom_type == "episodic"

    def test_episodic_temporal(self):
        atoms = decompose("Yesterday the deployment failed due to a missing env var.")
        assert atoms[0].atom_type == "episodic"

    def test_procedural_always(self):
        atoms = decompose("Always validate user input before passing it to the database.")
        assert atoms[0].atom_type == "procedural"

    def test_procedural_never(self):
        atoms = decompose("Never store passwords in plaintext.")
        assert atoms[0].atom_type == "procedural"

    def test_procedural_should(self):
        atoms = decompose("You should specify dtype when calling read_csv.")
        assert atoms[0].atom_type == "procedural"

    def test_procedural_to_prevent(self):
        atoms = decompose("To prevent injection attacks, use parameterised queries.")
        assert atoms[0].atom_type == "procedural"

    def test_semantic_default(self):
        atoms = decompose("PostgreSQL uses MVCC for transaction isolation.")
        assert atoms[0].atom_type == "semantic"

    def test_semantic_general_fact(self):
        atoms = decompose("asyncpg returns rows as Record objects, not dicts.")
        assert atoms[0].atom_type == "semantic"

    def test_short_fragments_skipped(self):
        atoms = decompose("OK. Sure. Yes.")
        assert atoms == []


# ── Confidence inference ──────────────────────────────────────────────────────

class TestConfidenceInference:
    def test_episodic_is_high_confidence(self):
        atoms = decompose("I confirmed the fix works in production.")
        assert atoms[0].confidence_alpha == 8.0
        assert atoms[0].confidence_beta == 1.0

    def test_procedural_is_moderate(self):
        atoms = decompose("Always use parameterised queries.")
        assert atoms[0].confidence_alpha == 4.0
        assert atoms[0].confidence_beta == 2.0

    def test_semantic_is_moderate(self):
        atoms = decompose("Redis keys expire based on TTL settings.")
        assert atoms[0].confidence_alpha == 4.0
        assert atoms[0].confidence_beta == 2.0

    def test_hedging_reduces_confidence(self):
        atoms = decompose("I think maybe the cache is causing this issue.")
        assert atoms[0].confidence_beta > atoms[0].confidence_alpha

    def test_uncertain_language(self):
        atoms = decompose("It could be that the index is missing.")
        # very low confidence
        assert atoms[0].confidence_beta >= 3.0

    def test_verified_increases_confidence(self):
        atoms = decompose("I verified this definitely works on Python 3.12.")
        assert atoms[0].confidence_alpha == 8.0
        assert atoms[0].confidence_beta == 1.0


# ── Merge adjacent ────────────────────────────────────────────────────────────

class TestMergeAdjacent:
    def test_adjacent_same_type_merged(self):
        # Both sentences trigger the episodic pattern ("I found", "I noticed")
        atoms = decompose("I found the bug in the auth module. I noticed it only on POST requests.")
        assert len(atoms) == 1
        assert atoms[0].atom_type == "episodic"
        assert "found" in atoms[0].text
        assert "noticed" in atoms[0].text

    def test_different_types_not_merged(self):
        text = (
            "asyncpg is an async PostgreSQL driver. "
            "I discovered a connection leak yesterday."
        )
        atoms = decompose(text)
        types = [a.atom_type for a in atoms]
        assert "semantic" in types
        assert "episodic" in types
        assert len(atoms) == 2

    def test_merge_takes_max_alpha(self):
        # Two episodic sentences — merged atom should keep higher alpha
        text = "I discovered the issue. I noticed it happens on every request."
        atoms = decompose(text)
        assert len(atoms) == 1
        assert atoms[0].confidence_alpha == 8.0


# ── Edge inference ────────────────────────────────────────────────────────────

class TestEdgeInference:
    def test_spec_example_edges(self):
        """Canonical spec example: episodic→semantic, procedural→semantic."""
        text = (
            "pandas.read_csv silently coerces mixed-type columns. "
            "I discovered this processing client_data.csv when row 847 had a string. "
            "Always specify dtype explicitly when using read_csv."
        )
        atoms = decompose(text)
        edges = infer_edges(atoms)

        type_map = {i: a.atom_type for i, a in enumerate(atoms)}
        edge_triples = {
            (type_map[s], type_map[t], et) for s, t, et in edges
        }

        assert ("episodic", "semantic", "evidence_for") in edge_triples
        assert ("procedural", "semantic", "motivated_by") in edge_triples

    def test_episodic_to_procedural_without_semantic(self):
        """If no semantic atom, episodic should link to procedural."""
        text = (
            "I ran into a timezone issue. "
            "Always store timestamps in UTC."
        )
        atoms = decompose(text)
        types = [a.atom_type for a in atoms]
        # Should have episodic and procedural but not semantic
        assert "episodic" in types
        assert "procedural" in types

        edges = infer_edges(atoms)
        type_map = {i: a.atom_type for i, a in enumerate(atoms)}
        edge_triples = {(type_map[s], type_map[t], et) for s, t, et in edges}

        assert ("episodic", "procedural", "evidence_for") in edge_triples

    def test_no_self_edges(self):
        """Edges should never point from an atom to itself."""
        text = "I found a null pointer exception. Always check for None before indexing."
        atoms = decompose(text)
        edges = infer_edges(atoms)
        for src, tgt, _ in edges:
            assert src != tgt

    def test_no_edges_single_atom(self):
        atoms = decompose("asyncpg does not auto-cast Python dicts to JSONB.")
        edges = infer_edges(atoms)
        assert edges == []


# ── Structured extraction ─────────────────────────────────────────────────────

class TestStructuredExtraction:
    def test_extracts_inline_code(self):
        atoms = decompose("Use `asyncpg.create_pool` instead of per-request connections.")
        assert atoms[0].structured.get("code") == "asyncpg.create_pool"

    def test_no_code_returns_empty(self):
        atoms = decompose("Always use connection pools for database access.")
        assert atoms[0].structured == {}


# ── Classification order tests ────────────────────────────────────────────────

class TestClassificationOrder:
    def test_procedural_beats_first_person(self):
        """'I will always X' — procedural signal wins over first-person voice."""
        atoms = decompose("I will always use parameterised queries.")
        assert atoms[0].atom_type == "procedural"

    def test_never_with_first_person_is_procedural(self):
        """'I never X' — 'never' fires procedural before episodic check.
        This is a known limitation: 'I never understood why' is ambiguous
        but classified as procedural. Acceptable for v0.1 rule-based approach.
        """
        atoms = decompose("I never understood why this works.")
        assert atoms[0].atom_type == "procedural"  # known limitation, documented

    def test_dotted_identifier_not_split(self):
        """torch.nn.Module should not be split into two sentences."""
        atoms = decompose("Use torch.nn.Module for custom layers.")
        assert len(atoms) == 1
        assert "torch.nn.Module" in atoms[0].text

    def test_pandas_identifier_preserved(self):
        """pd.read_csv inside a sentence should survive the sentence splitter."""
        atoms = decompose(
            "I discovered that pd.read_csv silently coerces types. "
            "Always specify dtype explicitly."
        )
        assert any("pd.read_csv" in a.text for a in atoms)
        types = [a.atom_type for a in atoms]
        assert "episodic" in types
        assert "procedural" in types

    def test_multi_level_dotted_identifier(self):
        """os.path.join (two dots) should not be split."""
        atoms = decompose("Always use os.path.join instead of string concatenation.")
        assert len(atoms) == 1
        assert "os.path.join" in atoms[0].text


# ── Full spec example (smoke test) ───────────────────────────────────────────

def test_spec_example_full():
    """The complete spec example should decompose into 3 typed atoms."""
    text = (
        "pandas.read_csv silently coerces mixed-type columns. "
        "I discovered this processing client_data.csv when row 847 had a string "
        "in the account_id column. "
        "From now on I should always specify dtype explicitly when using read_csv."
    )
    atoms = decompose(text)
    types = [a.atom_type for a in atoms]

    assert "semantic" in types
    assert "episodic" in types
    assert "procedural" in types

    # Episodic should be high confidence (direct experience)
    episodic = next(a for a in atoms if a.atom_type == "episodic")
    assert episodic.confidence_alpha == 8.0

    # Edges should be present
    edges = infer_edges(atoms)
    assert len(edges) >= 2
