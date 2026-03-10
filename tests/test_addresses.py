"""Tests for agent address validation, building, and resolution."""

import pytest
import pytest_asyncio
from fastapi import HTTPException
from uuid import UUID, uuid4

from mnemo.server.services.address_service import (
    validate_address,
    build_address,
    resolve_address,
    resolve_agent_identifier,
    create_address,
)


class TestAddressValidation:
    """Unit tests for validate_address and build_address."""

    def test_valid_simple(self):
        assert validate_address("clio:tom.inforge") is True

    def test_valid_with_hyphens(self):
        assert validate_address("my-agent:some-user.some-org") is True

    def test_valid_with_numbers(self):
        assert validate_address("agent1:user2.org3") is True

    def test_uppercase_normalized(self):
        assert validate_address("Clio:Tom.Inforge") is True

    def test_invalid_no_colon(self):
        assert validate_address("cliotom.inforge") is False

    def test_invalid_no_dot(self):
        assert validate_address("clio:tominforge") is False

    def test_invalid_spaces(self):
        assert validate_address("clio:tom .inforge") is False

    def test_invalid_underscores(self):
        assert validate_address("my_agent:tom.inforge") is False

    def test_invalid_empty_parts(self):
        assert validate_address(":tom.inforge") is False
        assert validate_address("clio:.inforge") is False
        assert validate_address("clio:tom.") is False

    def test_invalid_leading_trailing_hyphens(self):
        assert validate_address("-clio:tom.inforge") is False
        assert validate_address("clio:-tom.inforge") is False
        assert validate_address("clio:tom.-inforge") is False

    def test_max_length_exceeded(self):
        long_name = "a" * 100
        long_addr = f"{long_name}:{long_name}.{long_name}"
        assert len(long_addr) > 200
        assert validate_address(long_addr) is False

    def test_max_length_within_limit(self):
        addr = "a" * 50 + ":" + "b" * 50 + "." + "c" * 50
        assert len(addr) <= 200
        assert validate_address(addr) is True

    def test_build_address(self):
        assert build_address("clio", "tom", "inforge") == "clio:tom.inforge"

    def test_build_address_lowercased(self):
        assert build_address("Clio", "Tom", "Inforge") == "clio:tom.inforge"


@pytest.mark.asyncio
class TestAddressResolution:
    """Integration tests for address resolution against the database."""

    async def test_resolve_found(self, pool, agent_with_address):
        agent = agent_with_address
        result = await resolve_address(pool, agent["address"])
        assert result == agent["id"]

    async def test_resolve_not_found(self, pool, clean_db):
        result = await resolve_address(pool, "nonexistent:user.org")
        assert result is None

    async def test_resolve_agent_identifier_with_uuid(self, pool, agent_with_address):
        agent = agent_with_address
        uuid_str = str(agent["id"])
        result = await resolve_agent_identifier(pool, uuid_str)
        assert result == agent["id"]

    async def test_resolve_agent_identifier_with_address(self, pool, agent_with_address):
        agent = agent_with_address
        result = await resolve_agent_identifier(pool, agent["address"])
        assert result == agent["id"]

    async def test_resolve_agent_identifier_not_found(self, pool, clean_db):
        with pytest.raises(HTTPException) as exc_info:
            await resolve_agent_identifier(pool, "missing:user.org")
        assert exc_info.value.status_code == 404
