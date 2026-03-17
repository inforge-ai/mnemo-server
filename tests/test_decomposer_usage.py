"""Tests for decomposer token usage logging."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import remember


class TestLLMDecomposerUsageReturn:
    """Verify llm_decompose returns usage metadata alongside atoms."""

    @pytest.mark.asyncio
    async def test_returns_decomposer_result_with_usage(self):
        """llm_decompose returns a DecomposerResult with atoms and usage."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Test fact", "type": "semantic", "confidence": 0.8},
        ]))]
        mock_response.usage = MagicMock(
            input_tokens=150,
            output_tokens=42,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=0,
        )
        mock_response.model = "claude-haiku-4-5-20251001"

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            from mnemo.server.llm_decomposer import llm_decompose
            result = await llm_decompose("Test fact")

        assert hasattr(result, "atoms")
        assert hasattr(result, "usage")
        assert len(result.atoms) == 1
        assert result.atoms[0].text == "Test fact"
        assert result.usage is not None
        assert result.usage["model"] == "claude-haiku-4-5-20251001"
        assert result.usage["input_tokens"] == 150
        assert result.usage["output_tokens"] == 42
        assert result.usage["cache_creation_input_tokens"] == 100
        assert result.usage["cache_read_input_tokens"] == 0

    @pytest.mark.asyncio
    async def test_usage_handles_missing_cache_fields(self):
        """Cache token fields are None when not present on the response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "A fact", "type": "semantic", "confidence": 0.7},
        ]))]
        mock_response.usage = MagicMock(
            input_tokens=80,
            output_tokens=30,
            spec=["input_tokens", "output_tokens"],
        )
        # Remove cache attributes so getattr returns None
        del mock_response.usage.cache_creation_input_tokens
        del mock_response.usage.cache_read_input_tokens
        mock_response.model = "claude-haiku-4-5-20251001"

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            from mnemo.server.llm_decomposer import llm_decompose
            result = await llm_decompose("A fact")

        assert result.usage["input_tokens"] == 80
        assert result.usage["cache_creation_input_tokens"] is None
        assert result.usage["cache_read_input_tokens"] is None

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_result(self):
        """Empty input returns DecomposerResult with no atoms and no usage."""
        from mnemo.server.llm_decomposer import llm_decompose
        result = await llm_decompose("")
        assert result.atoms == []
        assert result.usage is None


class TestDecomposerUsageLogging:
    """Integration tests: verify usage rows are written to the database."""

    async def test_llm_decomposer_logs_usage_to_db(self, client, agent, pool):
        """When LLM decomposer is active, a decomposer_usage row is created."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "PostgreSQL supports JSONB", "type": "semantic", "confidence": 0.9},
        ]))]
        mock_response.usage = MagicMock(
            input_tokens=200,
            output_tokens=50,
            cache_creation_input_tokens=120,
            cache_read_input_tokens=0,
        )
        mock_response.model = "claude-haiku-4-5-20251001"

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            await remember(client, agent["id"], "PostgreSQL supports JSONB")

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM decomposer_usage WHERE agent_id = $1",
                agent["id"],
            )
        assert len(rows) == 1
        row = rows[0]
        assert row["model"] == "claude-haiku-4-5-20251001"
        assert row["input_tokens"] == 200
        assert row["output_tokens"] == 50
        assert row["cache_creation_input_tokens"] == 120
        assert row["cache_read_input_tokens"] == 0
        assert row["operator_id"] is not None
        assert row["store_id"] is not None

    async def test_regex_decomposer_does_not_log_usage(self, client, agent, pool):
        """When regex decomposer is used (no API key), no usage row is created."""
        import os
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            await remember(client, agent["id"], "Regex decomposer test sentence.")
        finally:
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM decomposer_usage WHERE agent_id = $1",
                agent["id"],
            )
        assert count == 0
