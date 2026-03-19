"""Tests for cross-call edge inference (linking atoms across /remember calls).

Test sentences are chosen to have specific cosine similarities with gte-small:
- Related pairs: 0.85-0.89 (above 0.78 threshold, below 0.90 dedup threshold)
- Unrelated pairs: ~0.70 (below 0.78 threshold)
"""

import pytest
from tests.conftest import remember


class TestCrossCallEdges:
    """Atoms stored in separate /remember calls should get edges when similar."""

    async def test_creates_edges_between_similar_atoms_across_calls(self, client, agent, pool):
        """Two /remember calls about related topics should produce cross-call edges."""
        # These sentences have ~0.89 cosine similarity (above 0.78, below 0.90 dedup)
        await remember(client, agent["id"],
            "pgvector uses HNSW indexes for approximate nearest neighbor search.")
        await remember(client, agent["id"],
            "Indexing strategies for vector databases include IVFFlat and HNSW approaches.")

        # Verify cross-call edges exist by checking edges between atoms from different calls
        async with pool.acquire() as conn:
            cross_edges = await conn.fetch(
                """
                SELECT e.edge_type, e.weight
                FROM edges e
                JOIN atoms a1 ON a1.id = e.source_id
                JOIN atoms a2 ON a2.id = e.target_id
                WHERE a1.agent_id = $1
                  AND a1.created_at != a2.created_at
                """,
                agent["id"],
            )
        assert len(cross_edges) >= 1, "Expected at least one cross-call edge between related atoms"

    async def test_no_edges_between_unrelated_atoms_across_calls(self, client, agent, pool):
        """Unrelated topics across calls should NOT produce cross-call edges."""
        await remember(client, agent["id"],
            "pgvector uses HNSW indexes for approximate nearest neighbor search.")
        await remember(client, agent["id"],
            "My grandmother's apple pie recipe uses cinnamon and brown sugar.")

        async with pool.acquire() as conn:
            cross_edges = await conn.fetch(
                """
                SELECT e.id
                FROM edges e
                JOIN atoms a1 ON a1.id = e.source_id
                JOIN atoms a2 ON a2.id = e.target_id
                WHERE a1.agent_id = $1
                  AND a1.created_at != a2.created_at
                """,
                agent["id"],
            )
        assert len(cross_edges) == 0, "Unrelated atoms should not have cross-call edges"

    async def test_cross_call_edges_use_related_type(self, client, agent, pool):
        """Cross-call edges should be of type 'related' with valid weight."""
        # ~0.85 similarity
        await remember(client, agent["id"],
            "Memory consolidation reduces noise in the knowledge graph over time.")
        await remember(client, agent["id"],
            "Graph databases benefit from periodic maintenance to remove stale edges.")

        async with pool.acquire() as conn:
            edges = await conn.fetch(
                """
                SELECT e.edge_type, e.weight
                FROM edges e
                JOIN atoms a1 ON a1.id = e.source_id
                JOIN atoms a2 ON a2.id = e.target_id
                WHERE a1.agent_id = $1
                  AND a1.created_at != a2.created_at
                """,
                agent["id"],
            )
        assert len(edges) >= 1
        for edge in edges:
            assert edge["edge_type"] == "related"
            assert 0.0 < edge["weight"] <= 1.0

    async def test_cross_call_edges_do_not_duplicate(self, client, agent, pool):
        """ON CONFLICT DO NOTHING prevents duplicate cross-call edges."""
        await remember(client, agent["id"],
            "pgvector uses HNSW indexes for approximate nearest neighbor search.")
        await remember(client, agent["id"],
            "Indexing strategies for vector databases include IVFFlat and HNSW approaches.")
        # Third call related to both — should not create duplicate edges
        await remember(client, agent["id"],
            "Vector search indexes like HNSW provide sublinear query time for high-dimensional data.")

        async with pool.acquire() as conn:
            dupes = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT source_id, target_id, edge_type, COUNT(*) AS cnt
                    FROM edges e
                    JOIN atoms a ON a.id = e.source_id
                    WHERE a.agent_id = $1
                    GROUP BY source_id, target_id, edge_type
                    HAVING COUNT(*) > 1
                ) sub
                """,
                agent["id"],
            )
        assert dupes == 0, "Edge uniqueness constraint should prevent duplicates"

    async def test_cross_call_edges_only_for_same_agent(self, client, pool, clean_db):
        """Cross-call edges should not link atoms across different agents."""
        r1 = await client.post("/v1/agents", json={"name": "agent-a", "domain_tags": ["test"]})
        r2 = await client.post("/v1/agents", json={"name": "agent-b", "domain_tags": ["test"]})
        agent_a = r1.json()
        agent_b = r2.json()

        await remember(client, agent_a["id"],
            "pgvector uses HNSW indexes for approximate nearest neighbor search.")
        await remember(client, agent_b["id"],
            "Indexing strategies for vector databases include IVFFlat and HNSW approaches.")

        async with pool.acquire() as conn:
            cross_agent_edges = await conn.fetch(
                """
                SELECT e.id
                FROM edges e
                JOIN atoms a1 ON a1.id = e.source_id
                JOIN atoms a2 ON a2.id = e.target_id
                WHERE a1.agent_id != a2.agent_id
                """,
            )
        assert len(cross_agent_edges) == 0, "Cross-call edges must not span agents"
