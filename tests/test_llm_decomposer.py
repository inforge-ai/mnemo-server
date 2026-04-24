# tests/test_llm_decomposer.py
"""Tests for the LLM decomposer."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from mnemo.server.llm_decomposer import llm_decompose


class TestLLMDecomposer:
    """Unit tests for llm_decompose — no API calls, mocked Anthropic client."""

    @pytest.mark.asyncio
    async def test_basic_decomposition(self):
        """LLM decomposer returns DecomposerResult with atoms from API response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Mnemo uses Beta distributions for confidence", "type": "semantic", "confidence": 0.9},
            {"text": "Expected confidence is alpha/(alpha+beta)", "type": "semantic", "confidence": 0.85},
        ]))]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Mnemo uses Beta distributions for confidence. Expected confidence is alpha/(alpha+beta).")

        atoms = result.atoms
        assert len(atoms) == 2
        assert atoms[0].text == "Mnemo uses Beta distributions for confidence"
        assert atoms[0].atom_type == "semantic"
        assert atoms[0].confidence_alpha == 8.0  # >= 0.8 → Beta(8,1)
        assert atoms[0].confidence_beta == 1.0
        assert atoms[1].text == "Expected confidence is alpha/(alpha+beta)"

    @pytest.mark.asyncio
    async def test_confidence_mapping_high(self):
        """Confidence >= 0.8 maps to Beta(8, 1)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "This is certain", "confidence": 0.95},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("This is certain")

        assert result.atoms[0].confidence_alpha == 8.0
        assert result.atoms[0].confidence_beta == 1.0

    @pytest.mark.asyncio
    async def test_confidence_mapping_moderate(self):
        """Confidence 0.6-0.8 maps to Beta(4, 2)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "A known fact", "confidence": 0.7},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("A known fact")

        assert result.atoms[0].confidence_alpha == 4.0
        assert result.atoms[0].confidence_beta == 2.0

    @pytest.mark.asyncio
    async def test_confidence_mapping_low(self):
        """Confidence 0.25-0.4 maps to Beta(2, 3)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Maybe this is true", "confidence": 0.3},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Maybe this is true")

        assert result.atoms[0].confidence_alpha == 2.0
        assert result.atoms[0].confidence_beta == 3.0

    @pytest.mark.asyncio
    async def test_confidence_mapping_very_low(self):
        """Confidence < 0.25 maps to Beta(2, 4)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "I have no idea if this is right", "confidence": 0.15},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("I have no idea if this is right")

        assert result.atoms[0].confidence_alpha == 2.0
        assert result.atoms[0].confidence_beta == 4.0

    @pytest.mark.asyncio
    async def test_empty_input(self):
        """Empty or whitespace input returns empty DecomposerResult without API call."""
        mock_client = AsyncMock()

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("")

        assert result.atoms == []
        assert result.usage is None
        mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_falls_back_to_regex(self):
        """API errors trigger regex fallback instead of raising."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("I learned that asyncpg is fast. Always use connection pools.")

        # Should return atoms from regex fallback, not raise
        assert len(result.atoms) >= 1
        assert result.usage is None  # No LLM usage since it fell back

    @pytest.mark.asyncio
    async def test_uses_prompt_caching(self):
        """System prompt uses cache_control for Anthropic prompt caching."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "test", "confidence": 0.5},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            await llm_decompose("test text")

        call_kwargs = mock_client.messages.create.call_args[1]
        system_msg = call_kwargs["system"][0]
        assert system_msg["cache_control"] == {"type": "ephemeral"}
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


class TestTypeClassification:
    """Tests for LLM type classification (episodic/semantic/procedural)."""

    @pytest.mark.asyncio
    async def test_type_classification_procedural(self):
        """Procedural type is mapped from LLM response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Always run migrations before deploying", "type": "procedural", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Always run migrations before deploying.")

        assert result.atoms[0].atom_type == "procedural"

    @pytest.mark.asyncio
    async def test_type_classification_episodic(self):
        """Episodic type is mapped from LLM response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "I found a deadlock when running the batch job yesterday", "type": "episodic", "confidence": 0.8},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("I found a deadlock when running the batch job yesterday.")

        assert result.atoms[0].atom_type == "episodic"

    @pytest.mark.asyncio
    async def test_type_classification_semantic(self):
        """Semantic type is mapped from LLM response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "PostgreSQL uses MVCC for concurrent access", "type": "semantic", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("PostgreSQL uses MVCC for concurrent access.")

        assert result.atoms[0].atom_type == "semantic"

    @pytest.mark.asyncio
    async def test_mixed_input_produces_mixed_types(self):
        """Mixed input returns atoms with at least two distinct types."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "I discovered that row 847 had a string in the account_id column", "type": "episodic", "confidence": 0.85},
            {"text": "pandas.read_csv silently coerces mixed-type columns", "type": "semantic", "confidence": 0.9},
            {"text": "Always specify dtype explicitly when using read_csv", "type": "procedural", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose(
                "I discovered that row 847 had a string in the account_id column. "
                "pandas.read_csv silently coerces mixed-type columns. "
                "Always specify dtype explicitly when using read_csv."
            )

        types = {a.atom_type for a in result.atoms}
        assert len(types) >= 2

    @pytest.mark.asyncio
    async def test_invalid_type_falls_back_to_semantic(self):
        """Invalid type string falls back to semantic."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Some fact", "type": "declarative", "confidence": 0.7},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Some fact")

        assert result.atoms[0].atom_type == "semantic"

    @pytest.mark.asyncio
    async def test_missing_type_falls_back_to_semantic(self):
        """Missing type field falls back to semantic."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Some fact", "confidence": 0.7},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Some fact")

        assert result.atoms[0].atom_type == "semantic"


class TestStateClaimBackstop:
    """Tests for the state-claim backstop that downgrades semantic → episodic
    when the LLM mis-classifies a time-scoped state/plan as a timeless fact."""

    @pytest.mark.asyncio
    async def test_zulip_planned_downgrades_to_episodic(self):
        """The Ticket 4 motivating case: 'X is planned' is episodic, not semantic."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Zulip integration is planned as a future pair-programming task",
             "type": "semantic", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Zulip integration is planned as a future pair-programming task.")

        assert result.atoms[0].atom_type == "episodic"

    @pytest.mark.asyncio
    async def test_is_currently_downgrades_to_episodic(self):
        """'As of' state claims downgrade to episodic."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Sampo is currently the project scheduler",
             "type": "semantic", "confidence": 0.85},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Sampo is currently the project scheduler.")

        assert result.atoms[0].atom_type == "episodic"

    @pytest.mark.asyncio
    async def test_has_not_yet_been_downgrades_to_episodic(self):
        """'Has not yet been' is a time-scoped negative claim, not a timeless fact."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "The BAM interview has not yet been completed",
             "type": "semantic", "confidence": 0.8},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("The BAM interview has not yet been completed.")

        assert result.atoms[0].atom_type == "episodic"

    @pytest.mark.asyncio
    async def test_timeless_semantic_stays_semantic(self):
        """Timeless facts with no state-claim markers are not touched."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "CPython's GIL serialises bytecode execution",
             "type": "semantic", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("CPython's GIL serialises bytecode execution.")

        assert result.atoms[0].atom_type == "semantic"

    @pytest.mark.asyncio
    async def test_general_semantic_fact_stays_semantic(self):
        """Another timeless fact — 'uses' is not a state-claim marker."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Beancount uses double-entry accounting",
             "type": "semantic", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Beancount uses double-entry accounting.")

        assert result.atoms[0].atom_type == "semantic"

    @pytest.mark.asyncio
    async def test_llm_episodic_is_not_touched(self):
        """Atoms already tagged episodic by the LLM are never rewritten by the backstop."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Zulip integration is planned as a future task",
             "type": "episodic", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Zulip integration is planned as a future task.")

        assert result.atoms[0].atom_type == "episodic"

    @pytest.mark.asyncio
    async def test_procedural_with_state_language_stays_procedural(self):
        """Procedural atoms are never rewritten by the backstop (only semantic is)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Always check whether the dependency is currently installed",
             "type": "procedural", "confidence": 0.9},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("Always check whether the dependency is currently installed.")

        assert result.atoms[0].atom_type == "procedural"


class TestStateClaimPatterns:
    """Direct tests for _looks_like_state_claim — make the matched patterns explicit."""

    def test_is_planned_matches(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert _looks_like_state_claim("Zulip integration is planned for next quarter")

    def test_is_scheduled_matches(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert _looks_like_state_claim("The deployment is scheduled for Friday")

    def test_is_currently_matches(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert _looks_like_state_claim("Sampo is currently the project scheduler")

    def test_is_the_current_matches(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert _looks_like_state_claim("Mnemo is the current memory system")

    def test_has_not_yet_been_matches(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert _looks_like_state_claim("The migration has not yet been applied")

    def test_on_the_roadmap_matches(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert _looks_like_state_claim("Graph expansion is on the roadmap")

    def test_plain_fact_does_not_match(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        assert not _looks_like_state_claim("CPython's GIL serialises bytecode execution")

    def test_past_event_does_not_match(self):
        from mnemo.server.llm_decomposer import _looks_like_state_claim
        # Past events are episodic but don't need the backstop — the LLM tags them correctly
        assert not _looks_like_state_claim("Tom deployed the server on 2026-04-15")


class TestEntityResolution:
    """Tests for the entity_resolved flag — atoms with unresolved definite-article
    references to generic nouns ('the test run', 'the project') should have their
    initial confidence degraded one band so decay eats them faster and recall
    deprioritises them."""

    @pytest.mark.asyncio
    async def test_resolved_entity_high_confidence_passes_through(self):
        """entity_resolved=true at high confidence lands at (8, 1) as usual."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "In the ABACAB March 2026 test run, test tasks consumed 89% of spend",
             "type": "episodic", "confidence": 0.9, "entity_resolved": True},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("source text")

        assert result.atoms[0].confidence_alpha == 8.0
        assert result.atoms[0].confidence_beta == 1.0

    @pytest.mark.asyncio
    async def test_unresolved_entity_degrades_confidence_one_band(self):
        """entity_resolved=false degrades 0.9 → 0.7, landing at (4, 2) not (8, 1)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "In the test run, test tasks consumed 89% of spend",
             "type": "episodic", "confidence": 0.9, "entity_resolved": False},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("source text")

        assert result.atoms[0].confidence_alpha == 4.0
        assert result.atoms[0].confidence_beta == 2.0

    @pytest.mark.asyncio
    async def test_unresolved_mid_band_degrades_further(self):
        """Degradation is proportional: 0.65 → 0.45 drops from (4, 2) to (3, 2)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "In the thing, stuff happened",
             "type": "episodic", "confidence": 0.65, "entity_resolved": False},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("source text")

        assert result.atoms[0].confidence_alpha == 3.0
        assert result.atoms[0].confidence_beta == 2.0

    @pytest.mark.asyncio
    async def test_missing_entity_resolved_field_treats_as_resolved(self):
        """Backwards-compatible: absent `entity_resolved` defaults to true."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "CPython's GIL serialises bytecode execution",
             "type": "semantic", "confidence": 0.85},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose("source text")

        assert result.atoms[0].confidence_alpha == 8.0
        assert result.atoms[0].confidence_beta == 1.0

    @pytest.mark.asyncio
    async def test_ambiguous_case_two_projects_stay_separate(self):
        """Review-notes ambiguous case — a paragraph mentioning both ABACAB and
        Sampo deployments produces atoms that each identify the correct project
        (mocked at the LLM layer; we verify the decomposer preserves the LLM's
        entity-resolved text rather than collapsing to 'the deployment')."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "The ABACAB deployment failed with a timeout",
             "type": "episodic", "confidence": 0.9, "entity_resolved": True},
            {"text": "The Sampo deployment failed with a permission error",
             "type": "episodic", "confidence": 0.9, "entity_resolved": True},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            result = await llm_decompose(
                "Two deployments failed today: ABACAB timed out; Sampo hit a permission error."
            )

        assert len(result.atoms) == 2
        texts = [a.text for a in result.atoms]
        assert any("ABACAB" in t for t in texts), f"no ABACAB atom: {texts}"
        assert any("Sampo" in t for t in texts), f"no Sampo atom: {texts}"
        # Neither atom should have been left with a generic "the deployment"
        assert not any(t.lower().startswith("the deployment ") and "abacab" not in t.lower() and "sampo" not in t.lower() for t in texts)


class TestDecomposerIntegration:
    """Test that the correct decomposer is selected based on ANTHROPIC_API_KEY."""

    @pytest.mark.asyncio
    async def test_falls_back_to_regex_without_api_key(self):
        """Without ANTHROPIC_API_KEY, _decompose uses the regex decomposer."""
        from mnemo.server.services.atom_service import _decompose
        import os

        # Ensure no API key
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            result = await _decompose("The sky is blue.")
            assert len(result.atoms) >= 1
            # Regex decomposer returns DecomposedAtom objects
            assert hasattr(result.atoms[0], "text")
            assert hasattr(result.atoms[0], "atom_type")
        finally:
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key
