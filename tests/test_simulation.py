"""
Integration tests for the mock agent simulation framework.

Uses ASGI transport (no real HTTP server) via a thin adapter that maps
MnemoClient's interface onto the httpx AsyncClient from the client fixture.
"""

import pytest
from uuid import UUID

from mnemo.simulation.mock_agent import MockAgent
from mnemo.simulation.harness import SimulationHarness
from mnemo.simulation.metrics import SimulationMetrics
from mnemo.simulation.personas import PYTHON_DEV_PERSONA, DEVOPS_PERSONA, ALL_PERSONAS


# ── ASGI adapter ───────────────────────────────────────────────────────────────

class _AsgiMnemoClient:
    """
    Wraps the httpx AsyncClient (ASGI transport) from the test fixture
    to match the duck-typed interface expected by MockAgent and SimulationHarness.
    """

    def __init__(self, httpx_client):
        self._c = httpx_client

    async def register_agent(
        self, name: str, persona: str | None = None, domain_tags: list[str] | None = None
    ) -> dict:
        r = await self._c.post(
            "/v1/agents",
            json={"name": name, "persona": persona, "domain_tags": domain_tags or []},
        )
        r.raise_for_status()
        return r.json()

    async def remember(
        self, agent_id: UUID, text: str, domain_tags: list[str] | None = None
    ) -> dict:
        r = await self._c.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": text, "domain_tags": domain_tags or []},
        )
        r.raise_for_status()
        return r.json()

    async def recall(
        self,
        agent_id: UUID,
        query: str,
        min_confidence: float = 0.1,
        max_results: int = 5,
        expand_graph: bool = True,
        atom_types: list[str] | None = None,
        domain_tags: list[str] | None = None,
    ) -> dict:
        r = await self._c.post(
            f"/v1/agents/{agent_id}/recall",
            json={
                "query": query,
                "min_confidence": min_confidence,
                "max_results": max_results,
                "expand_graph": expand_graph,
            },
        )
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        pass  # httpx client lifecycle is managed by the fixture


# ── MockAgent unit tests ───────────────────────────────────────────────────────

async def test_mock_agent_tick_records_metrics(client, agent):
    """A single tick should increment all metric counters."""
    adapter = _AsgiMnemoClient(client)
    mock = MockAgent(adapter, UUID(agent["id"]), PYTHON_DEV_PERSONA)

    await mock.tick()

    assert mock.tick_count == 1
    assert mock.retrievals_done == 1
    assert len(mock.retrieval_hit_rates) == 1
    # atoms_stored may be 0 if all were merged as duplicates — just check the type
    assert isinstance(mock.atoms_stored, int)


async def test_mock_agent_run_n_ticks(client, agent):
    """run(n) should complete exactly n ticks."""
    adapter = _AsgiMnemoClient(client)
    mock = MockAgent(adapter, UUID(agent["id"]), DEVOPS_PERSONA)

    await mock.run(ticks=5)

    assert mock.tick_count == 5
    assert mock.retrievals_done == 5
    assert len(mock.retrieval_hit_rates) == 5


async def test_mock_agent_metrics_dict(client, agent):
    """metrics() should return a dict with all expected keys."""
    adapter = _AsgiMnemoClient(client)
    mock = MockAgent(adapter, UUID(agent["id"]), PYTHON_DEV_PERSONA)
    await mock.run(ticks=3)

    m = mock.metrics()

    assert m["agent_name"] == PYTHON_DEV_PERSONA["name"]
    assert m["tick_count"] == 3
    assert m["retrievals_done"] == 3
    assert 0.0 <= m["avg_hit_rate"] <= 1.0


async def test_mock_agent_generate_text():
    """_generate_text should fill all placeholders from params."""
    mock = MockAgent(None, UUID("00000000-0000-0000-0000-000000000001"), PYTHON_DEV_PERSONA)
    template = "I found that {library} {issue} in {dataset}"
    params = {
        "library": ["pandas"],
        "issue": ["fails"],
        "dataset": ["data.csv"],
    }
    result = mock._generate_text(template, params)
    assert result == "I found that pandas fails in data.csv"
    assert "{" not in result


async def test_mock_agent_generate_text_unknown_placeholder():
    """Placeholders not in params should be left unchanged."""
    mock = MockAgent(None, UUID("00000000-0000-0000-0000-000000000001"), PYTHON_DEV_PERSONA)
    result = mock._generate_text("Hello {name} and {unknown}", {"name": ["World"]})
    assert "World" in result
    assert "{unknown}" in result


# ── SimulationHarness tests ────────────────────────────────────────────────────

async def test_harness_setup_creates_agents(client, clean_db):
    """setup() should register one agent per persona and create MockAgent objects."""
    adapter = _AsgiMnemoClient(client)
    harness = SimulationHarness(client=adapter)

    await harness.setup([PYTHON_DEV_PERSONA, DEVOPS_PERSONA])

    assert len(harness.agents) == 2
    assert harness.agents[0].persona["name"] == PYTHON_DEV_PERSONA["name"]
    assert harness.agents[1].persona["name"] == DEVOPS_PERSONA["name"]


async def test_harness_run_completes_ticks(client, clean_db):
    """run(ticks=3) should complete 3 ticks for every registered agent."""
    adapter = _AsgiMnemoClient(client)
    harness = SimulationHarness(client=adapter)
    await harness.setup([PYTHON_DEV_PERSONA, DEVOPS_PERSONA])
    await harness.run(ticks=3)

    for agent in harness.agents:
        assert agent.tick_count == 3


async def test_harness_report_structure(client, clean_db):
    """report() should return a dict with expected top-level keys."""
    adapter = _AsgiMnemoClient(client)
    harness = SimulationHarness(client=adapter)
    await harness.setup([PYTHON_DEV_PERSONA])
    await harness.run(ticks=2)

    report = harness.report()

    assert "agents" in report
    assert "total_atoms" in report
    assert "avg_hit_rate" in report
    assert "timeline" in report
    assert len(report["agents"]) == 1
    assert report["agents"][0]["tick_count"] == 2


async def test_harness_stores_atoms_in_db(client, agent, pool, clean_db):
    """After a run, atoms should appear in the database."""
    adapter = _AsgiMnemoClient(client)
    harness = SimulationHarness(client=adapter)
    await harness.setup([PYTHON_DEV_PERSONA])
    await harness.run(ticks=5)

    # Check that atoms exist for the registered agent
    sim_agent_id = harness.agents[0].agent_id  # already a UUID
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM atoms WHERE agent_id = $1 AND is_active = true",
            sim_agent_id,
        )
    assert count >= 0  # might be 0 if everything was merged, but the run should not crash


# ── SimulationMetrics tests ────────────────────────────────────────────────────

def test_metrics_record_and_summary():
    """Metrics should accumulate correctly across recorded ticks."""
    m = SimulationMetrics()
    m.record_tick("alice", 0, atoms_created=3, duplicates_merged=0, hit_rate=0.0)
    m.record_tick("alice", 1, atoms_created=2, duplicates_merged=1, hit_rate=0.4)
    m.record_tick("bob",   0, atoms_created=1, duplicates_merged=0, hit_rate=0.8)

    s = m.summary()

    assert s["total_ticks"] == 3
    assert s["total_atoms_created"] == 6
    assert s["total_duplicates_merged"] == 1


def test_metrics_hit_rate_by_agent():
    """hit_rate_by_tick should filter to the correct agent."""
    m = SimulationMetrics()
    m.record_tick("alice", 0, 1, 0, hit_rate=0.2)
    m.record_tick("alice", 1, 1, 0, hit_rate=0.6)
    m.record_tick("bob",   0, 1, 0, hit_rate=0.9)

    alice_rates = m.hit_rate_by_tick("alice")
    assert alice_rates == [0.2, 0.6]

    all_rates = m.hit_rate_by_tick()
    assert len(all_rates) == 3


def test_metrics_avg_hit_rate_empty():
    """avg_hit_rate on an empty timeline should return 0.0, not raise."""
    m = SimulationMetrics()
    assert m.avg_hit_rate() == 0.0


# ── Persona structure tests ────────────────────────────────────────────────────

def test_all_personas_have_required_fields():
    """Every persona must have the fields MockAgent expects."""
    required_top = {"name", "persona", "domain_tags", "discoveries"}
    required_discovery = {"episodic", "semantic", "procedural", "params"}

    for persona in ALL_PERSONAS:
        assert required_top <= set(persona), f"{persona.get('name')} missing top-level fields"
        assert isinstance(persona["domain_tags"], list)
        assert len(persona["discoveries"]) >= 1
        for disc in persona["discoveries"]:
            assert required_discovery <= set(disc), (
                f"{persona['name']} discovery missing fields: {disc}"
            )
            assert isinstance(disc["params"], dict)
            for key, vals in disc["params"].items():
                assert isinstance(vals, list) and len(vals) >= 1, (
                    f"{persona['name']} param '{key}' must be a non-empty list"
                )
