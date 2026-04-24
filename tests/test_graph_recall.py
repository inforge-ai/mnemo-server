# tests/test_graph_recall.py
"""Integration tests for Ticket 2 — graph-aware recall.

These tests use explicit SQL fixture setup so the graph structure is fully
controlled. The /remember + /recall round-trip is already covered by the
existing recall tests; here we want tight assertions on match_type, via,
scoring, and the ceiling.
"""

import asyncio
import pytest
from uuid import uuid4

from mnemo.server.config import settings
from mnemo.server.embeddings import encode
from tests.conftest import remember


async def _insert_atom(conn, agent_id, text, alpha=8.0, beta=1.0, domain_tags=("test",)):
    """Insert a single atom with a freshly-computed embedding and return its row."""
    emb = await encode(text)
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, structured, embedding,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, decay_half_life_days, decay_type, decomposer_version
        ) VALUES ($1, 'semantic', $2, '{}'::jsonb, $3::vector, $4, $5,
                  'direct_experience', $6, 30.0, 'none', 'test_v1')
        RETURNING id, text_content
        """,
        agent_id, text, emb, alpha, beta, list(domain_tags),
    )
    return row


async def _insert_edge(conn, source_id, target_id, weight=1.0, edge_type="related"):
    await conn.execute(
        """
        INSERT INTO edges (source_id, target_id, edge_type, weight)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
        """,
        source_id, target_id, edge_type, weight,
    )


class TestGraphRecallMechanics:
    """Behavioural guarantees on the new match_type / via / ceiling behaviour."""

    @pytest.mark.asyncio
    async def test_graph_match_surfaces_edge_connected_atom(self, client, agent, pool):
        """Hermes-motivating case: a vector query hits atom A; atom B is
        edge-linked to A but does not itself match the query well. Graph
        expansion should surface B with match_type=graph and via=A."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            cost_atom = await _insert_atom(
                conn, aid,
                "Test tasks consumed 89% of total spend during the March 2026 test run",
            )
            project_atom = await _insert_atom(
                conn, aid,
                "ABACAB is the project under which the March 2026 test run was conducted",
            )
            await _insert_edge(conn, cost_atom["id"], project_atom["id"], weight=1.0)

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={
                "query": "test run spend costs",
                "min_similarity": 0.2,
                "max_results": 5,
                "expand_graph": True,
            },
            headers=ag_headers,
        )
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]

        by_type: dict[str, list[dict]] = {"vector": [], "graph": []}
        for a in atoms:
            if a.get("match_type") in by_type:
                by_type[a["match_type"]].append(a)

        assert by_type["vector"], f"no vector matches: {atoms}"
        # Graph match must appear with a via pointing at a vector match
        graph_matches = by_type["graph"]
        assert graph_matches, f"no graph matches surfaced: {atoms}"
        vector_ids = {a["id"] for a in by_type["vector"]}
        for g in graph_matches:
            assert g["via"] is not None
            assert g["via"] in vector_ids

    @pytest.mark.asyncio
    async def test_graph_atoms_never_outrank_source(self, client, agent, pool):
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            a = await _insert_atom(conn, aid, "Python dicts preserve insertion order since 3.7")
            b = await _insert_atom(conn, aid, "CPython 3.6 introduced the compact dict representation")
            await _insert_edge(conn, a["id"], b["id"], weight=0.9)

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "Python dictionary insertion order",
                  "min_similarity": 0.2, "max_results": 5, "expand_graph": True},
            headers=ag_headers,
        )
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        by_id = {a["id"]: a for a in atoms}
        for atom in atoms:
            if atom.get("match_type") == "graph":
                source = by_id.get(atom["via"])
                if source is not None:
                    assert atom["relevance_score"] <= source["relevance_score"]

    @pytest.mark.asyncio
    async def test_vector_match_not_duplicated_as_graph(self, client, agent, pool):
        """If an atom is both a vector match and graph-reachable, it must
        appear exactly once, with match_type=vector."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            a = await _insert_atom(conn, aid, "PostgreSQL uses MVCC for concurrent access")
            b = await _insert_atom(conn, aid, "MVCC enables Postgres to serve readers without blocking writers")
            await _insert_edge(conn, a["id"], b["id"], weight=1.0)

        # Query matching both atoms strongly so both are likely vector matches.
        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "PostgreSQL MVCC concurrent",
                  "min_similarity": 0.2, "max_results": 10, "expand_graph": True},
            headers=ag_headers,
        )
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        seen = set()
        for atom in atoms:
            assert atom["id"] not in seen, f"atom {atom['id']} appeared twice"
            seen.add(atom["id"])
        # Any atom that IS a vector match should not also be a graph match
        vector_ids = {a["id"] for a in atoms if a.get("match_type") == "vector"}
        graph_ids = {a["id"] for a in atoms if a.get("match_type") == "graph"}
        assert vector_ids.isdisjoint(graph_ids)

    @pytest.mark.asyncio
    async def test_ceiling_caps_graph_expansion(self, client, agent, pool):
        """Graph expansion yields at most (ceiling_multiplier × max_results) atoms.
        With max_results=2 and default multiplier=2, ceiling is 4."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            seed = await _insert_atom(conn, aid, "Postgres is an open-source database")
            # Create 10 edge-linked atoms
            for i in range(10):
                neighbor = await _insert_atom(conn, aid, f"Postgres feature number {i}: durability and friends")
                await _insert_edge(conn, seed["id"], neighbor["id"], weight=1.0)

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "Postgres open-source database",
                  "min_similarity": 0.2, "max_results": 2, "expand_graph": True},
            headers=ag_headers,
        )
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        graph_count = sum(1 for a in atoms if a.get("match_type") == "graph")
        ceiling = 2 * settings.graph_recall_expansion_ceiling_multiplier
        assert graph_count <= ceiling, f"graph matches {graph_count} exceeded ceiling {ceiling}"

    @pytest.mark.asyncio
    async def test_discount_demotes_graph_scores(self, client, agent, pool, monkeypatch):
        """Lowering graph_recall_edge_discount reduces graph-match relevance_score."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            a = await _insert_atom(conn, aid, "Redis uses single-threaded event loop")
            b = await _insert_atom(conn, aid, "Event loops process commands serially in Redis")
            await _insert_edge(conn, a["id"], b["id"], weight=1.0)

        async def recall():
            r = await client.post(
                f"/v1/agents/{aid}/recall",
                json={"query": "Redis concurrency model",
                      "min_similarity": 0.2, "max_results": 5, "expand_graph": True},
                headers=ag_headers,
            )
            return r.json()["atoms"]

        # Default discount
        atoms_default = await recall()
        graph_default = [a for a in atoms_default if a.get("match_type") == "graph"]

        # Halve the discount
        monkeypatch.setattr(settings, "graph_recall_edge_discount", 0.1)
        atoms_low = await recall()
        graph_low = [a for a in atoms_low if a.get("match_type") == "graph"]

        # Same graph atoms should appear but with lower scores
        if graph_default and graph_low:
            by_id_default = {a["id"]: a for a in graph_default}
            for g in graph_low:
                if g["id"] in by_id_default:
                    assert g["relevance_score"] < by_id_default[g["id"]]["relevance_score"]

    @pytest.mark.asyncio
    async def test_no_graph_matches_when_no_edges(self, client, agent, pool):
        """With no edges, only vector matches come back — no graph matches, no errors."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            await _insert_atom(conn, aid, "The Linux kernel uses a CFS scheduler by default")

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "Linux scheduler CFS",
                  "min_similarity": 0.2, "max_results": 5, "expand_graph": True},
            headers=ag_headers,
        )
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert all(a.get("match_type") != "graph" for a in atoms)
