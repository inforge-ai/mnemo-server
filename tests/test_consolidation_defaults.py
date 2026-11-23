"""
v2 Phase 0: with default settings, consolidation is non-destructive and recall
does not apply a confidence floor.

- A faded atom stays active (decay step off).
- A same-type duplicate pair stays as two active atoms (merge step off).
- No "Generalised" atoms are created (generalise step off).
- An edge to an inactive atom survives (prune step off).
- Recall returns a low-confidence atom even when the client sends
  min_confidence=0.1 (recall_confidence_floor_enabled off).
"""

from uuid import UUID

from mnemo.server.config import settings
from mnemo.server.embeddings import encode
from mnemo.server.services.consolidation import run_consolidation
from tests.test_consolidation import _insert_atom_sql


def test_destructive_flags_default_off():
    from mnemo.server.config import Settings
    fields = Settings.model_fields
    for flag in (
        "consolidation_decay_enabled",
        "consolidation_generalise_enabled",
        "consolidation_merge_enabled",
        "consolidation_prune_edges_enabled",
        "recall_confidence_floor_enabled",
    ):
        assert fields[flag].default is False, flag
        assert getattr(settings, flag) is False, flag


async def test_default_consolidation_is_non_destructive(client, agent, pool):
    agent_id = UUID(agent["id"])
    emb_faded = await encode("The staging cluster was rebooted after the kernel patch.")
    emb_dup = await encode("Deploys to production happen on Tuesdays and Thursdays.")
    emb_x = await encode("The retry budget for the ingest worker is five attempts.")
    emb_y = await encode("Grafana alerts route to the on-call pager channel.")

    async with pool.acquire() as conn:
        faded = await _insert_atom_sql(
            conn, agent_id, "episodic", "Staging cluster rebooted.", emb_faded,
            half_life=1.0,
        )
        await conn.execute(
            "UPDATE atoms SET created_at = now() - interval '3 years', "
            "last_accessed = NULL, access_count = 0 WHERE id = $1",
            faded,
        )
        # Identical embeddings → cosine 1.0, well above any merge threshold.
        dup_a = await _insert_atom_sql(conn, agent_id, "semantic", "Deploys Tue/Thu.", emb_dup, decay_type="none")
        dup_b = await _insert_atom_sql(conn, agent_id, "semantic", "Deploys Tue+Thu.", emb_dup, decay_type="none")
        dup_c = await _insert_atom_sql(conn, agent_id, "semantic", "Deploys on Tue and Thu.", emb_dup, decay_type="none")

        live = await _insert_atom_sql(conn, agent_id, "semantic", "Retry budget is five.", emb_x, decay_type="none")
        dead = await _insert_atom_sql(conn, agent_id, "semantic", "Alerts go to pager.", emb_y, decay_type="none")
        await conn.execute(
            "INSERT INTO edges (source_id, target_id, edge_type, weight) VALUES ($1, $2, 'supports', 1.0)",
            live, dead,
        )
        await conn.execute("UPDATE atoms SET is_active = false WHERE id = $1", dead)

        before = await conn.fetchval("SELECT count(*) FROM atoms WHERE agent_id = $1", agent_id)

    result = await run_consolidation(pool)
    assert result["decayed"] == 0
    assert result["clustered"] == 0
    assert result["merged"] == 0
    assert result["pruned"] == 0

    async with pool.acquire() as conn:
        for atom_id in (faded, dup_a, dup_b, dup_c, live):
            assert await conn.fetchval("SELECT is_active FROM atoms WHERE id = $1", atom_id) is True
        after = await conn.fetchval("SELECT count(*) FROM atoms WHERE agent_id = $1", agent_id)
        assert after == before  # no generalised atoms created
        edges = await conn.fetchval(
            "SELECT count(*) FROM edges WHERE source_id = $1 AND target_id = $2", live, dead,
        )
        assert edges == 1
        # Audit row still written, with zeros.
        meta = await conn.fetchval(
            "SELECT metadata FROM access_log WHERE action = 'consolidation' ORDER BY created_at DESC LIMIT 1"
        )
        assert meta is not None


async def test_recall_ignores_confidence_floor_by_default(client, agent, pool):
    agent_id = UUID(agent["id"])
    headers = {"X-Agent-Key": agent["agent_key"]}
    text = "The billing service migrated its ledger to a double-entry schema."
    emb = await encode(text)
    async with pool.acquire() as conn:
        atom_id = await _insert_atom_sql(
            conn, agent_id, "semantic", text, emb,
            confidence_alpha=1.0, confidence_beta=30.0, decay_type="none",
        )
        eff = await conn.fetchval(
            """SELECT effective_confidence(confidence_alpha, confidence_beta, decay_type,
                   decay_half_life_days, created_at, last_accessed, access_count)
               FROM atoms WHERE id = $1""",
            atom_id,
        )
    assert eff < 0.1  # below what clients send as min_confidence

    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "billing ledger double-entry schema", "min_confidence": 0.1, "expand_graph": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()["atoms"]]
    assert str(atom_id) in ids


async def test_recall_floor_applies_when_enabled(client, agent, pool, monkeypatch):
    monkeypatch.setattr(settings, "recall_confidence_floor_enabled", True)
    agent_id = UUID(agent["id"])
    headers = {"X-Agent-Key": agent["agent_key"]}
    text = "The billing service migrated its ledger to a double-entry schema."
    emb = await encode(text)
    async with pool.acquire() as conn:
        atom_id = await _insert_atom_sql(
            conn, agent_id, "semantic", text, emb,
            confidence_alpha=1.0, confidence_beta=30.0, decay_type="none",
        )
    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "billing ledger double-entry schema", "min_confidence": 0.1, "expand_graph": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()["atoms"]]
    assert str(atom_id) not in ids
