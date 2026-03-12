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
        """LLM decomposer returns DecomposedAtom list from API response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Mnemo uses Beta distributions for confidence", "confidence": 0.9},
            {"text": "Expected confidence is alpha/(alpha+beta)", "confidence": 0.85},
        ]))]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("Mnemo uses Beta distributions for confidence. Expected confidence is alpha/(alpha+beta).")

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
            atoms = await llm_decompose("This is certain")

        assert atoms[0].confidence_alpha == 8.0
        assert atoms[0].confidence_beta == 1.0

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
            atoms = await llm_decompose("A known fact")

        assert atoms[0].confidence_alpha == 4.0
        assert atoms[0].confidence_beta == 2.0

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
            atoms = await llm_decompose("Maybe this is true")

        assert atoms[0].confidence_alpha == 2.0
        assert atoms[0].confidence_beta == 3.0

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
            atoms = await llm_decompose("I have no idea if this is right")

        assert atoms[0].confidence_alpha == 2.0
        assert atoms[0].confidence_beta == 4.0

    @pytest.mark.asyncio
    async def test_empty_input(self):
        """Empty or whitespace input returns empty list without API call."""
        mock_client = AsyncMock()

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("")

        assert atoms == []
        mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        """API errors propagate — caller handles them."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            with pytest.raises(Exception, match="API error"):
                await llm_decompose("Some text")

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
