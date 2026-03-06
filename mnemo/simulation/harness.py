"""
SimulationHarness — orchestrates a multi-agent simulation run.

Usage:
    harness = SimulationHarness(client=my_mnemo_client)
    await harness.setup([PYTHON_DEV_PERSONA, DEVOPS_PERSONA])
    await harness.run(ticks=20)
    print(harness.report())

The client must implement:
    await client.register_agent(name, persona, domain_tags) -> dict  (with "id" key)
    await client.remember(agent_id, text, domain_tags) -> dict
    await client.recall(agent_id, query, ...) -> dict
    await client.close() (optional)
"""

from uuid import UUID

from .mock_agent import MockAgent
from .metrics import SimulationMetrics


class SimulationHarness:
    def __init__(self, client=None, base_url: str = "http://localhost:8000"):
        """
        Pass an existing client directly (for tests / ASGI transport), or
        leave client=None to have the harness create a MnemoClient from base_url.
        """
        self._provided_client = client
        self.base_url = base_url
        self.client = None
        self.agents: list[MockAgent] = []
        self.metrics = SimulationMetrics()

    async def setup(self, persona_defs: list[dict]) -> None:
        """Register one agent per persona and wire up MockAgent instances."""
        if self._provided_client is not None:
            self.client = self._provided_client
        else:
            from mnemo_client import MnemoClient
            self.client = MnemoClient(self.base_url, api_key="local-dev")

        for persona in persona_defs:
            agent_data = await self.client.register_agent(
                name=persona["name"],
                persona=persona.get("persona"),
                domain_tags=persona["domain_tags"],
            )
            agent_id = UUID(agent_data["id"])
            mock = MockAgent(self.client, agent_id, persona)
            self.agents.append(mock)

    async def run(self, ticks: int = 20) -> None:
        """Run all agents for the given number of ticks (round-robin)."""
        for tick_num in range(ticks):
            for agent in self.agents:
                result = await agent.tick()
                self.metrics.record_tick(
                    agent_name=agent.persona["name"],
                    tick=tick_num,
                    atoms_created=result.get("atoms_created", 0),
                    duplicates_merged=result.get("duplicates_merged", 0),
                    hit_rate=agent.retrieval_hit_rates[-1] if agent.retrieval_hit_rates else 0.0,
                )

    async def teardown(self) -> None:
        """Close the client if we created it."""
        if self._provided_client is None and self.client is not None:
            await self.client.close()

    def report(self) -> dict:
        """Return a summary dict of the simulation results."""
        agent_metrics = [a.metrics() for a in self.agents]
        total_atoms = sum(m["atoms_stored"] for m in agent_metrics)
        all_hit_rates = [m["avg_hit_rate"] for m in agent_metrics]
        avg_hit_rate = sum(all_hit_rates) / len(all_hit_rates) if all_hit_rates else 0.0

        return {
            "agents": agent_metrics,
            "total_agents": len(agent_metrics),
            "total_atoms": total_atoms,
            "avg_hit_rate": round(avg_hit_rate, 4),
            "timeline": self.metrics.timeline,
        }
