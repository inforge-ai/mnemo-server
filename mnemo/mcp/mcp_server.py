"""
Mnemo MCP Server — exposes Mnemo memory as MCP tools.

Tools:
  mnemo_remember  — Store a memory (free-text; server decomposes and links)
  mnemo_recall    — Search memories by semantic similarity
  mnemo_stats     — View memory statistics

Configuration (environment variables):
  MNEMO_BASE_URL       Mnemo REST API base URL (default: http://localhost:8000)
  MNEMO_AGENT_ID       UUID of existing agent to use (optional; auto-registers if absent)
  MNEMO_AGENT_NAME     Name used when auto-registering (default: mnemo-agent)
  MNEMO_AGENT_PERSONA  Persona string used when auto-registering (optional)
  MNEMO_DOMAIN_TAGS    Comma-separated default domain tags (optional)
  MNEMO_MCP_TRANSPORT  "stdio" (default) or "sse" for remote/network access
  MNEMO_MCP_HOST       Host to bind in SSE mode (default: 0.0.0.0)
  MNEMO_MCP_PORT       Port to bind in SSE mode (default: 8001)

Running (stdio — local/same machine):
  MNEMO_BASE_URL=http://localhost:8000 MNEMO_AGENT_NAME=claude \\
      python -m mnemo.mcp.mcp_server

Running (SSE — accessible over network/Tailscale):
  MNEMO_BASE_URL=http://localhost:8000 MNEMO_AGENT_NAME=claude \\
  MNEMO_MCP_TRANSPORT=sse MNEMO_MCP_PORT=8001 \\
      python -m mnemo.mcp.mcp_server

Claude Desktop config for SSE (remote/Tailscale):
  {
    "mcpServers": {
      "mnemo-memory": {
        "url": "http://<tailscale-ip>:8001/sse"
      }
    }
  }

Claude Desktop config for stdio (local — both Claude and Mnemo on same machine):
  {
    "mcpServers": {
      "mnemo-memory": {
        "command": "python",
        "args": ["-m", "mnemo.mcp.mcp_server"],
        "env": {
          "MNEMO_BASE_URL": "http://localhost:8000",
          "MNEMO_AGENT_NAME": "claude"
        }
      }
    }
  }
"""

import logging
import os
from contextlib import asynccontextmanager
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from mnemo.client.mnemo_client import MnemoClient

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

MNEMO_BASE_URL = os.environ.get("MNEMO_BASE_URL", "http://localhost:8000")
MNEMO_AGENT_ID = os.environ.get("MNEMO_AGENT_ID", "")
MNEMO_AGENT_NAME = os.environ.get("MNEMO_AGENT_NAME", "mnemo-agent")
MNEMO_AGENT_PERSONA = os.environ.get("MNEMO_AGENT_PERSONA", "")
MNEMO_DOMAIN_TAGS = [
    t.strip() for t in os.environ.get("MNEMO_DOMAIN_TAGS", "").split(",") if t.strip()
]
MNEMO_MCP_TRANSPORT = os.environ.get("MNEMO_MCP_TRANSPORT", "stdio")
MNEMO_MCP_HOST = os.environ.get("MNEMO_MCP_HOST", "0.0.0.0")
MNEMO_MCP_PORT = int(os.environ.get("MNEMO_MCP_PORT", "8001"))


# ── State ─────────────────────────────────────────────────────────────────────

_client: MnemoClient | None = None
_agent_id: UUID | None = None


async def _get_client() -> tuple[MnemoClient, UUID]:
    """Return the shared (client, agent_id) pair, initialising on first call."""
    global _client, _agent_id
    if _client is None:
        _client = MnemoClient(MNEMO_BASE_URL)
        _agent_id = await _resolve_agent(_client)
    return _client, _agent_id


async def _resolve_agent(client: MnemoClient) -> UUID:
    """Return the configured agent's UUID, registering a new one if needed."""
    if MNEMO_AGENT_ID:
        agent_id = UUID(MNEMO_AGENT_ID)
        try:
            await client.get_agent(agent_id)
            logger.info("Using existing agent %s", agent_id)
            return agent_id
        except Exception:
            logger.warning("Agent %s not found, registering a new one", agent_id)

    agent = await client.register_agent(
        name=MNEMO_AGENT_NAME,
        persona=MNEMO_AGENT_PERSONA or None,
        domain_tags=MNEMO_DOMAIN_TAGS,
    )
    agent_id = UUID(agent["id"])
    logger.info("Registered new agent '%s' → %s", MNEMO_AGENT_NAME, agent_id)
    return agent_id


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    "mnemo-memory",
    instructions=(
        "Mnemo is your persistent memory. "
        "Use mnemo_remember to store what you learn. "
        "Use mnemo_recall to search what you know. "
        "Use mnemo_stats to see your memory state."
    ),
    host=MNEMO_MCP_HOST,
    port=MNEMO_MCP_PORT,
)


@mcp.tool(
    description=(
        "Store a memory. Describe what happened, what you learned, or a rule to follow. "
        "Mnemo handles classification (episodic/semantic/procedural), confidence estimation, "
        "and graph linking automatically."
    ),
)
async def mnemo_remember(
    text: str,
    domain_tags: list[str] | None = None,
) -> str:
    """
    Args:
        text: What to remember. Be specific — include context, outcomes, and lessons
              learned. Multi-sentence input is decomposed into typed atoms.
        domain_tags: Optional topic tags to organise memories (e.g. ["python", "debugging"]).
    """
    client, agent_id = await _get_client()
    result = await client.remember(
        agent_id=agent_id,
        text=text,
        domain_tags=domain_tags or [],
    )
    parts = [f"Stored {result['atoms_created']} memories"]
    if result["edges_created"]:
        parts.append(f"{result['edges_created']} connections")
    if result["duplicates_merged"]:
        parts.append(f"{result['duplicates_merged']} duplicates merged")
    return ", ".join(parts) + "."


@mcp.tool(
    description=(
        "Search your memories by semantic similarity. Returns relevant memories ranked "
        "by similarity and confidence, plus related knowledge via graph expansion."
    ),
)
async def mnemo_recall(
    query: str,
    domain_tags: list[str] | None = None,
    max_results: int = 5,
    min_similarity: float = 0.2,
) -> str:
    """
    Args:
        query: What you're looking for. Descriptive phrases work better than keywords —
               e.g. "how to handle CSV type coercion in pandas" not just "pandas".
        domain_tags: Optional filter to specific domains.
        max_results: Maximum number of primary results to return (default 5).
        min_similarity: Minimum cosine similarity to query (default 0.2). Raise to
                        tighten results; lower to broaden them.
    """
    client, agent_id = await _get_client()
    result = await client.recall(
        agent_id=agent_id,
        query=query,
        domain_tags=domain_tags,
        max_results=max_results,
        min_confidence=0.1,
        min_similarity=min_similarity,
        expand_graph=True,
    )
    atoms = result.get("atoms", [])
    expanded = result.get("expanded_atoms", [])

    if not atoms and not expanded:
        return "No relevant memories found."

    lines = []
    for atom in atoms:
        conf = atom.get("confidence_effective", 0.0)
        conf_label = "high" if conf > 0.7 else "moderate" if conf > 0.4 else "low"
        score = atom.get("relevance_score")
        score_str = f", {score:.2f}" if score is not None else ""
        lines.append(
            f"[{atom['atom_type']}] ({conf_label} conf{score_str}) {atom['text_content']}"
        )

    if expanded:
        lines.append("— Related —")
        for atom in expanded[:3]:
            score = atom.get("relevance_score")
            score_str = f" ({score:.2f})" if score is not None else ""
            lines.append(f"[{atom['atom_type']}]{score_str} {atom['text_content']}")

    return "\n".join(lines)


@mcp.tool(
    description="View your memory statistics: total atoms, active count, confidence, and graph density.",
)
async def mnemo_stats() -> str:
    """Returns a summary of the current agent's memory state."""
    client, agent_id = await _get_client()
    s = await client.stats(agent_id=agent_id)
    lines = [
        f"Total memories : {s['total_atoms']} (active: {s['active_atoms']})",
        f"By type        : {s.get('atoms_by_type', {})}",
        f"Arc atoms      : {s.get('arc_atoms', 0)}",
        f"Avg confidence : {s.get('avg_effective_confidence', 0.0):.0%}",
        f"Edges          : {s.get('total_edges', 0)}",
        f"Views          : {s.get('active_views', 0)}",
        f"Granted access : {s.get('granted_capabilities', 0)}",
        f"Received access: {s.get('received_capabilities', 0)}",
    ]
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if MNEMO_MCP_TRANSPORT == "sse":
        logger.info("Starting MCP server (SSE) on %s:%d", MNEMO_MCP_HOST, MNEMO_MCP_PORT)
    mcp.run(transport=MNEMO_MCP_TRANSPORT)


if __name__ == "__main__":
    main()
