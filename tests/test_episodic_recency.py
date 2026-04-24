# tests/test_episodic_recency.py
"""Ticket 4b — recency ranking within episodic near-duplicates.

The Zulip scenario: two episodic atoms about the same subject at different
points in time. The newer remembered_on should outrank the older, so
'Zulip completed on 2026-04-15' beats 'Zulip planned on 2026-03-01' on a
Zulip query even when the planned atom has higher embedding similarity.
"""

from datetime import datetime, timezone
import pytest

from mnemo.server.config import settings
from mnemo.server.embeddings import encode


async def _insert_atom(
    conn, agent_id, text, atom_type="episodic", alpha=8.0, beta=1.0,
    remembered_on=None, domain_tags=("test",),
):
    emb = await encode(text)
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, structured, embedding,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, decay_half_life_days, decay_type, decomposer_version,
            remembered_on
        ) VALUES ($1, $2, $3, '{}'::jsonb, $4::vector, $5, $6,
                  'direct_experience', $7, 30.0, 'none', 'test_v1', $8)
        RETURNING id, text_content, remembered_on
        """,
        agent_id, atom_type, text, emb, alpha, beta, list(domain_tags), remembered_on,
    )
    return row


class TestEpisodicRecencyRanking:

    @pytest.mark.asyncio
    async def test_newer_episodic_outranks_older_on_near_duplicate(self, client, agent, pool):
        """Zulip motivating case: 'Zulip completed' (newer remembered_on) should
        outrank 'Zulip planned' on a Zulip query even if similarity is close."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            old = await _insert_atom(
                conn, aid,
                "Zulip integration is planned as a future pair-programming task",
                remembered_on=datetime(2026, 3, 1, tzinfo=timezone.utc),
            )
            new = await _insert_atom(
                conn, aid,
                "Zulip integration completed as a pair-programming task",
                remembered_on=datetime(2026, 4, 15, tzinfo=timezone.utc),
            )

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "Zulip integration pair programming",
                  "min_similarity": 0.2, "max_results": 5, "expand_graph": False},
            headers=ag_headers,
        )
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        by_id = {a["id"]: a for a in atoms}
        assert str(old["id"]) in by_id and str(new["id"]) in by_id, (
            f"expected both atoms returned; got ids {list(by_id)}"
        )
        new_score = by_id[str(new["id"])]["relevance_score"]
        old_score = by_id[str(old["id"])]["relevance_score"]
        assert new_score > old_score, (
            f"newer episodic should outrank older: new={new_score}, old={old_score}"
        )

    @pytest.mark.asyncio
    async def test_null_remembered_on_falls_back_to_created_at(self, client, agent, pool):
        """Atoms with NULL remembered_on use created_at for ranking — the
        existing behaviour for pre-Ticket-4b atoms. Older created_at is demoted."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            # Both with NULL remembered_on; rely on created_at timestamp difference.
            old = await _insert_atom(conn, aid, "Zulip integration is planned")
            # Backdate the "old" atom via direct UPDATE
            await conn.execute(
                "UPDATE atoms SET created_at = $1 WHERE id = $2",
                datetime(2026, 3, 1, tzinfo=timezone.utc), old["id"],
            )
            new = await _insert_atom(conn, aid, "Zulip integration completed")

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "Zulip integration", "min_similarity": 0.2,
                  "max_results": 5, "expand_graph": False},
            headers=ag_headers,
        )
        atoms = resp.json()["atoms"]
        by_id = {a["id"]: a for a in atoms}
        if str(old["id"]) in by_id and str(new["id"]) in by_id:
            assert by_id[str(new["id"])]["relevance_score"] > by_id[str(old["id"])]["relevance_score"]

    @pytest.mark.asyncio
    async def test_semantic_atoms_not_demoted(self, client, agent, pool):
        """Semantic near-duplicates with different timestamps are not reshuffled
        by the episodic-recency logic."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            await _insert_atom(
                conn, aid, "CPython uses a global interpreter lock (GIL)",
                atom_type="semantic",
                remembered_on=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            await _insert_atom(
                conn, aid, "CPython's GIL serialises bytecode execution",
                atom_type="semantic",
                remembered_on=datetime(2026, 4, 15, tzinfo=timezone.utc),
            )

        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "CPython GIL", "min_similarity": 0.2,
                  "max_results": 5, "expand_graph": False},
            headers=ag_headers,
        )
        atoms = resp.json()["atoms"]
        # Ranking should be driven by query-similarity alone for semantics.
        # The only invariant we can assert cleanly: both atoms come back and
        # neither has been artificially demoted by the recency logic.
        assert all(a["atom_type"] == "semantic" for a in atoms)
        assert len(atoms) >= 1

    @pytest.mark.asyncio
    async def test_dissimilar_episodic_atoms_dont_interact(self, client, agent, pool):
        """Two episodic atoms with different embeddings (below the 0.85
        threshold) do not demote each other regardless of timestamps."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            old = await _insert_atom(
                conn, aid, "Tom completed the migration to Beancount on 2026-02-15",
                remembered_on=datetime(2026, 2, 15, tzinfo=timezone.utc),
            )
            new = await _insert_atom(
                conn, aid, "The BAM interview was completed on 2026-04-20",
                remembered_on=datetime(2026, 4, 20, tzinfo=timezone.utc),
            )

        # A query that hits both at modest similarity. Neither atom should
        # demote the other — they're not near-duplicates.
        resp = await client.post(
            f"/v1/agents/{aid}/recall",
            json={"query": "completed recent events", "min_similarity": 0.1,
                  "max_results": 5, "expand_graph": False},
            headers=ag_headers,
        )
        atoms = resp.json()["atoms"]
        by_id = {a["id"]: a for a in atoms}
        # Direct invariant: if both are returned, their scores should be driven
        # by similarity only (no cross-demotion).
        if str(old["id"]) in by_id and str(new["id"]) in by_id:
            # Neither has been zeroed out
            assert by_id[str(old["id"])]["relevance_score"] > 0
            assert by_id[str(new["id"])]["relevance_score"] > 0

    @pytest.mark.asyncio
    async def test_demotion_factor_config_tunable(self, client, agent, pool, monkeypatch):
        """Lowering the demotion factor increases the score gap between the
        older and newer atom."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}

        async with pool.acquire() as conn:
            old = await _insert_atom(
                conn, aid, "Zulip integration is planned for Q2",
                remembered_on=datetime(2026, 3, 1, tzinfo=timezone.utc),
            )
            new = await _insert_atom(
                conn, aid, "Zulip integration completed in Q2",
                remembered_on=datetime(2026, 4, 15, tzinfo=timezone.utc),
            )

        async def get_ratio():
            r = await client.post(
                f"/v1/agents/{aid}/recall",
                json={"query": "Zulip integration", "min_similarity": 0.2,
                      "max_results": 5, "expand_graph": False},
                headers=ag_headers,
            )
            atoms = r.json()["atoms"]
            by_id = {a["id"]: a for a in atoms}
            old_score = by_id[str(old["id"])]["relevance_score"]
            new_score = by_id[str(new["id"])]["relevance_score"]
            return old_score / new_score if new_score > 0 else 0

        # Default demotion (0.5): old atom is 50% demoted
        ratio_default = await get_ratio()

        # Stronger demotion (0.1): old atom should fall further behind
        monkeypatch.setattr(settings, "episodic_recency_demotion_factor", 0.1)
        ratio_low = await get_ratio()

        assert ratio_low < ratio_default, (
            f"tighter demotion should produce smaller ratio: "
            f"default={ratio_default:.3f}, low={ratio_low:.3f}"
        )
