# tests/test_recall_ranking.py
"""Tests for composite ranking and retrieval improvements."""

import pytest


class TestCompositeRanking:
    """Verify that recall ranks by composite score and returns relevance_score."""

    @pytest.mark.asyncio
    async def test_all_results_have_relevance_score(self, client, agent):
        """Every recalled atom should have a non-None relevance_score."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/remember", json={
            "text": "The PostgreSQL cosine distance operator is <=> and it is confirmed working correctly.",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "PostgreSQL distance operator",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) >= 1
        for atom in atoms:
            assert atom["relevance_score"] is not None
            assert atom["relevance_score"] > 0

    @pytest.mark.asyncio
    async def test_results_sorted_by_relevance_score(self, client, agent):
        """Results should be sorted by relevance_score descending."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/remember", json={
            "text": "Python is a programming language. PostgreSQL is a database. Redis is a cache.",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "database systems",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        if len(atoms) >= 2:
            for i in range(len(atoms) - 1):
                assert atoms[i]["relevance_score"] >= atoms[i + 1]["relevance_score"]


class TestPostRetrievalDedup:
    """Verify that near-duplicate atoms are collapsed in results."""

    @pytest.mark.asyncio
    async def test_dedup_collapses_near_identical_atoms(self, client, agent):
        """Two atoms with >0.95 cosine similarity should be collapsed to one."""
        aid = agent["id"]

        # Store two near-identical texts via direct atom creation to bypass decomposer
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "episodic",
            "text_content": "The deployment process requires running database migrations first",
        })
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": "The deployment process requires running database migrations first",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "deployment database migrations",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]

        # Dedup should collapse these — we should get 1 unique text, not 2
        texts = [a["text_content"] for a in atoms]
        assert texts.count("The deployment process requires running database migrations first") == 1

    @pytest.mark.asyncio
    async def test_dedup_keeps_distinct_atoms(self, client, agent):
        """Atoms with distinct content should not be collapsed."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": "PostgreSQL uses B-tree indexes by default",
        })
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Redis stores data in memory for fast access",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "database storage",
            "max_results": 10,
            "min_similarity": 0.0,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) == 2


class TestNoTypeFiltering:
    """Verify that atom_type is not used in retrieval filtering."""

    @pytest.mark.asyncio
    async def test_recall_returns_all_types(self, client, agent):
        """Recall should return atoms regardless of type."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "episodic",
            "text_content": "I observed the sky is blue today",
        })
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "procedural",
            "text_content": "Always check the sky color before going outside",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "sky color",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) == 2
        types = {a["atom_type"] for a in atoms}
        assert "episodic" in types
        assert "procedural" in types
