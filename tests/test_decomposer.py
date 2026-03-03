"""
Unit tests for the rule-based decomposer.
No database or async required.
"""

import pytest
from mnemo.server.decomposer import (
    decompose, infer_edges, DecomposedAtom,
    _compress_arc, _maybe_create_arc,
)


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
    """3-sentence input decomposes into 3 typed atoms + 1 arc, with 5 edges."""
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

    # One arc atom should be appended
    arc_atoms = [a for a in atoms if a.source_type == "arc"]
    assert len(arc_atoms) == 1
    assert len(atoms) == 4  # 3 decomposed + 1 arc

    # Episodic (non-arc) should be high confidence
    episodic = next(a for a in atoms if a.atom_type == "episodic" and a.source_type != "arc")
    assert episodic.confidence_alpha == 8.0

    # Edges: 2 original (evidence_for, motivated_by) + 3 summarises = 5
    edges = infer_edges(atoms)
    assert len(edges) == 5
    edge_types = [et for _, _, et in edges]
    assert edge_types.count("summarises") == 3


# ── Arc decomposer tests ──────────────────────────────────────────────────────

class TestArcDecomposer:
    def test_short_input_no_arc(self):
        atoms = decompose("asyncpg does not auto-cast Python dicts to JSONB.")
        assert all(a.source_type != "arc" for a in atoms)

    def test_two_sentence_no_arc(self):
        text = "asyncpg is fast. It uses binary protocol."
        atoms = decompose(text)
        assert all(a.source_type != "arc" for a in atoms)

    def test_medium_input_creates_arc(self):
        text = (
            "I discovered a memory leak in the connection pool. "
            "The leak only appeared under high concurrency. "
            "We fixed it by reducing the pool size."
        )
        atoms = decompose(text)
        arc_atoms = [a for a in atoms if a.source_type == "arc"]
        assert len(arc_atoms) == 1
        arc = arc_atoms[0]
        assert arc.atom_type == "episodic"
        assert arc.text == text  # full text for medium inputs

    def test_long_input_creates_compressed_arc(self):
        sentences = [
            "I started investigating the slow query issue on Monday.",
            "The database logs showed full table scans on the users table.",
            "Adding an index on email reduced query time from 800ms to 2ms.",
            "I also found that the ORM was generating N+1 queries.",
            "Switching to eager loading fixed the N+1 problem completely.",
            "The combined changes reduced average response time by 90 percent.",
            "I deployed the fix to staging and ran load tests successfully.",
            "Production deployment went smoothly with no rollback needed.",
        ]
        text = " ".join(sentences)
        atoms = decompose(text)
        arc_atoms = [a for a in atoms if a.source_type == "arc"]
        assert len(arc_atoms) == 1
        arc = arc_atoms[0]
        # Compressed arc must be shorter than the full text
        assert len(arc.text) < len(text)
        # Must contain first and last sentences
        assert sentences[0] in arc.text
        assert sentences[-1] in arc.text

    def test_arc_confidence_is_moderate(self):
        text = (
            "I noticed the cache was evicting entries too aggressively. "
            "The TTL was set to 60 seconds by default. "
            "Increasing TTL to 300 seconds improved hit rate. "
            "I verified the change in production last week."
        )
        atoms = decompose(text)
        arc = next(a for a in atoms if a.source_type == "arc")
        assert arc.confidence_alpha == 4.0
        assert arc.confidence_beta == 2.0

    def test_arc_inherits_no_structured(self):
        text = (
            "I discovered the issue with `pd.read_csv`. "
            "The dtype inference is unreliable on large files. "
            "Always specify dtype explicitly for production pipelines."
        )
        atoms = decompose(text)
        arc = next(a for a in atoms if a.source_type == "arc")
        assert arc.structured == {}

    def test_summarises_edge_type_in_infer_edges(self):
        text = (
            "I found the bug in the request handler. "
            "The handler was not validating the Content-Type header. "
            "Always validate headers before processing the request body."
        )
        atoms = decompose(text)
        edges = infer_edges(atoms)
        edge_types = [et for _, _, et in edges]
        assert "summarises" in edge_types

    def test_compression_deduplicates(self):
        # Make the first sentence also the longest so it appears twice in candidates
        long_first = "I discovered a critical performance regression in the database query path that affected all users."
        sentences = [
            long_first,
            "The issue was caused by a missing index.",
            "Adding the index resolved the problem.",
            "Tests passed on staging.",
            "Production deploy was successful.",
            "No rollback was needed.",
            "Monitoring shows normal latency now.",
        ]
        text = " ".join(sentences)
        compressed = _compress_arc(sentences)
        # First sentence should appear only once despite being both first and longest
        assert compressed.count(long_first) == 1
