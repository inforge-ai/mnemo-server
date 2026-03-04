"""
Lightweight async Python client for the Mnemo API.

Usage:
    async with MnemoClient("http://localhost:8000") as client:
        agent = await client.register_agent("my-agent",
            persona="python developer", domain_tags=["python"])

        result = await client.remember(
            agent_id=agent["id"],
            text="pandas.read_csv silently coerces mixed-type columns. "
                 "I discovered this processing client_data.csv. "
                 "From now on I should always specify dtype explicitly.",
            domain_tags=["python", "pandas"],
        )
        # result: {atoms_created: 3, edges_created: 2, duplicates_merged: 0}

        results = await client.recall(
            agent_id=agent["id"],
            query="loading CSV files with pandas",
        )

        view = await client.create_view(
            agent_id=agent["id"],
            name="pandas-csv-handling",
            atom_filter={"atom_types": ["procedural"], "domain_tags": ["pandas"]},
        )
        skill = await client.export_skill(agent_id=agent["id"], view_id=view["id"])

        cap = await client.grant(
            agent_id=agent["id"],
            view_id=view["id"],
            grantee_id=other_agent["id"],
        )
"""

import httpx
from typing import Optional
from uuid import UUID


class MnemoClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ── Primary Interface ──────────────────────────────────────────────────────

    async def remember(
        self,
        agent_id: UUID,
        text: str,
        domain_tags: Optional[list[str]] = None,
    ) -> dict:
        """Store a memory. Just say what happened. Server decomposes and links."""
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": text, "domain_tags": domain_tags or []},
        )
        resp.raise_for_status()
        return resp.json()

    async def recall(
        self,
        agent_id: UUID,
        query: str,
        atom_types: Optional[list[str]] = None,
        domain_tags: Optional[list[str]] = None,
        min_confidence: float = 0.1,
        min_similarity: float = 0.2,
        max_results: int = 10,
        expand_graph: bool = True,
        expansion_depth: int = 2,
        include_superseded: bool = False,
        similarity_drop_threshold: Optional[float] = 0.3,
        verbosity: str = "full",
        max_content_chars: int = 200,
        max_total_tokens: Optional[int] = None,
    ) -> dict:
        """Retrieve relevant memories by semantic search."""
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/recall",
            json={
                "query": query,
                "atom_types": atom_types,
                "domain_tags": domain_tags,
                "min_confidence": min_confidence,
                "min_similarity": min_similarity,
                "max_results": max_results,
                "expand_graph": expand_graph,
                "expansion_depth": expansion_depth,
                "include_superseded": include_superseded,
                "similarity_drop_threshold": similarity_drop_threshold,
                "verbosity": verbosity,
                "max_content_chars": max_content_chars,
                "max_total_tokens": max_total_tokens,
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ── Agent Management ───────────────────────────────────────────────────────

    async def register_agent(
        self,
        name: str,
        persona: Optional[str] = None,
        domain_tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        resp = await self.http.post(
            "/v1/agents",
            json={
                "name": name,
                "persona": persona,
                "domain_tags": domain_tags or [],
                "metadata": metadata or {},
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def find_agent_by_name(self, name: str) -> list[dict]:
        """Return active agents with this exact name (empty list if none found)."""
        resp = await self.http.get("/v1/agents", params={"name": name})
        resp.raise_for_status()
        return resp.json()

    async def get_agent(self, agent_id: UUID) -> dict:
        resp = await self.http.get(f"/v1/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()

    async def stats(self, agent_id: UUID) -> dict:
        resp = await self.http.get(f"/v1/agents/{agent_id}/stats")
        resp.raise_for_status()
        return resp.json()

    async def depart(self, agent_id: UUID) -> dict:
        """Initiate agent departure. Cascade-revokes all granted capabilities."""
        resp = await self.http.post(f"/v1/agents/{agent_id}/depart")
        resp.raise_for_status()
        return resp.json()

    # ── Power-User Atom Operations ─────────────────────────────────────────────

    async def store_atom(
        self,
        agent_id: UUID,
        atom_type: str,
        text_content: str,
        structured: Optional[dict] = None,
        confidence: Optional[str] = None,
        source_type: str = "direct_experience",
        domain_tags: Optional[list[str]] = None,
    ) -> dict:
        """Explicit atom creation (power-user interface)."""
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/atoms",
            json={
                "atom_type": atom_type,
                "text_content": text_content,
                "structured": structured or {},
                "confidence": confidence,
                "source_type": source_type,
                "domain_tags": domain_tags or [],
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_atom(self, agent_id: UUID, atom_id: UUID) -> dict:
        resp = await self.http.get(f"/v1/agents/{agent_id}/atoms/{atom_id}")
        resp.raise_for_status()
        return resp.json()

    async def delete_atom(self, agent_id: UUID, atom_id: UUID) -> None:
        resp = await self.http.delete(f"/v1/agents/{agent_id}/atoms/{atom_id}")
        resp.raise_for_status()

    async def link(
        self,
        agent_id: UUID,
        source_id: UUID,
        target_id: UUID,
        edge_type: str,
        weight: float = 1.0,
    ) -> dict:
        """Create a typed edge between two atoms."""
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/atoms/link",
            json={
                "source_id": str(source_id),
                "target_id": str(target_id),
                "edge_type": edge_type,
                "weight": weight,
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ── View Operations ────────────────────────────────────────────────────────

    async def create_view(
        self,
        agent_id: UUID,
        name: str,
        atom_filter: dict,
        description: Optional[str] = None,
    ) -> dict:
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/views",
            json={"name": name, "description": description, "atom_filter": atom_filter},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_views(self, agent_id: UUID) -> list[dict]:
        resp = await self.http.get(f"/v1/agents/{agent_id}/views")
        resp.raise_for_status()
        return resp.json()

    async def export_skill(self, agent_id: UUID, view_id: UUID) -> dict:
        resp = await self.http.get(
            f"/v1/agents/{agent_id}/views/{view_id}/export_skill"
        )
        resp.raise_for_status()
        return resp.json()

    # ── Capability Operations ──────────────────────────────────────────────────

    async def grant(
        self,
        agent_id: UUID,
        view_id: UUID,
        grantee_id: UUID,
        permissions: Optional[list[str]] = None,
        expires_at: Optional[str] = None,
    ) -> dict:
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/grant",
            json={
                "view_id": str(view_id),
                "grantee_id": str(grantee_id),
                "permissions": permissions or ["read"],
                "expires_at": expires_at,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def revoke(self, capability_id: UUID) -> dict:
        resp = await self.http.post(f"/v1/capabilities/{capability_id}/revoke")
        resp.raise_for_status()
        return resp.json()

    async def list_shared_views(self, agent_id: UUID) -> list[dict]:
        resp = await self.http.get(f"/v1/agents/{agent_id}/shared_views")
        resp.raise_for_status()
        return resp.json()

    async def recall_shared(
        self,
        agent_id: UUID,
        view_id: UUID,
        query: str,
        min_confidence: float = 0.1,
        max_results: int = 10,
        expand_graph: bool = True,
        expansion_depth: int = 2,
    ) -> dict:
        """Recall through a shared view (scope-bounded to snapshot atoms)."""
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/shared_views/{view_id}/recall",
            json={
                "query": query,
                "min_confidence": min_confidence,
                "max_results": max_results,
                "expand_graph": expand_graph,
                "expansion_depth": expansion_depth,
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ── Health ─────────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        resp = await self.http.get("/v1/health")
        resp.raise_for_status()
        return resp.json()
