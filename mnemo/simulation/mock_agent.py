"""
MockAgent — simulates an AI agent that uses Mnemo for memory.

The agent runs ticks where it:
  1. Recalls context relevant to a randomly-chosen discovery.
  2. Constructs natural-language memory text (episodic + semantic + procedural).
  3. Stores the memory via /remember.

The client object is duck-typed — it must implement:
  await client.remember(agent_id, text, domain_tags) -> dict
  await client.recall(agent_id, query, min_confidence, max_results, expand_graph) -> dict
"""

import random
from uuid import UUID


class MockAgent:
    def __init__(self, client, agent_id: UUID, persona: dict):
        self.mnemo = client
        self.agent_id = agent_id
        self.persona = persona
        self.domain_tags: list[str] = persona["domain_tags"]

        # Metrics
        self.tick_count: int = 0
        self.atoms_stored: int = 0
        self.duplicates_merged: int = 0
        self.retrievals_done: int = 0
        self.retrieval_hit_rates: list[float] = []

    # ── Core lifecycle ─────────────────────────────────────────────────────────

    async def tick(self) -> dict:
        """
        One cycle: recall context → compose memory text → remember learnings.
        Returns the result dict from /remember.
        """
        discovery = random.choice(self.persona["discoveries"])
        params = discovery["params"]

        # Phase 1: Recall relevant context
        query = self._generate_text(discovery["semantic"], params)
        context = await self.mnemo.recall(
            agent_id=self.agent_id,
            query=query,
            min_confidence=0.1,
            max_results=5,
            expand_graph=True,
        )
        self.retrievals_done += 1
        hit_count = len(context.get("atoms", []))
        self.retrieval_hit_rates.append(min(hit_count / 5.0, 1.0))

        # Phase 2: Compose natural-language memory (episodic + semantic + procedural)
        memory_text = (
            f"{self._generate_text(discovery['episodic'], params)}. "
            f"{self._generate_text(discovery['semantic'], params)}. "
            f"{self._generate_text(discovery['procedural'], params)}."
        )
        tags = random.sample(self.domain_tags, k=min(2, len(self.domain_tags)))

        result = await self.mnemo.remember(
            agent_id=self.agent_id,
            text=memory_text,
            domain_tags=tags,
        )

        self.atoms_stored += result.get("atoms_created", 0)
        self.duplicates_merged += result.get("duplicates_merged", 0)
        self.tick_count += 1
        return result

    async def run(self, ticks: int = 10) -> None:
        """Run the agent for a fixed number of ticks."""
        for _ in range(ticks):
            await self.tick()

    # ── Metrics ────────────────────────────────────────────────────────────────

    def metrics(self) -> dict:
        avg_hit = (
            sum(self.retrieval_hit_rates) / len(self.retrieval_hit_rates)
            if self.retrieval_hit_rates
            else 0.0
        )
        return {
            "agent_name": self.persona["name"],
            "agent_id": str(self.agent_id),
            "tick_count": self.tick_count,
            "atoms_stored": self.atoms_stored,
            "duplicates_merged": self.duplicates_merged,
            "retrievals_done": self.retrievals_done,
            "avg_hit_rate": round(avg_hit, 4),
        }

    # ── Text generation ────────────────────────────────────────────────────────

    def _generate_text(self, template: str, params: dict) -> str:
        """
        Fill {placeholder} slots in template with a random value from params[placeholder].
        Placeholders not present in params are left as-is.
        """
        result = template
        for key, values in params.items():
            placeholder = f"{{{key}}}"
            if placeholder in result:
                result = result.replace(placeholder, random.choice(values))
        return result
