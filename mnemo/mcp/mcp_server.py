"""
Mnemo MCP Server — exposes Mnemo memory as MCP tools.

Tools:
  mnemo_remember  — Store a memory (free-text; server decomposes and links)
  mnemo_recall    — Search memories by semantic similarity
  mnemo_stats     — View memory statistics

Configuration (environment variables):
  MNEMO_BASE_URL       Mnemo REST API base URL (default: http://localhost:8000)
  MNEMO_API_KEY        API key (preferred — use 'mnemo register' to generate)
  MNEMO_AGENT_NAME     Name used when auto-registering (default: mnemo-agent)
  MNEMO_AGENT_PERSONA  Persona string used when auto-registering (optional)
  MNEMO_DOMAIN_TAGS    Comma-separated default domain tags (optional)
  MNEMO_AGENT_ID       Deprecated — use MNEMO_API_KEY instead
  MNEMO_MCP_TRANSPORT  "stdio" (default) or "sse" for remote/network access
  MNEMO_MCP_HOST       Host to bind in SSE mode (default: 0.0.0.0)
  MNEMO_MCP_PORT       Port to bind in SSE mode (default: 8001)

Running (stdio — local/same machine):
  MNEMO_BASE_URL=http://localhost:8000 MNEMO_API_KEY=mnemo_... \\
      python -m mnemo.mcp.mcp_server

Running (SSE — accessible over network/Tailscale):
  MNEMO_BASE_URL=http://localhost:8000 MNEMO_API_KEY=mnemo_... \\
  MNEMO_MCP_TRANSPORT=sse MNEMO_MCP_PORT=8001 \\
      python -m mnemo.mcp.mcp_server

Claude Desktop config for stdio (local — both Claude and Mnemo on same machine):
  {
    "mcpServers": {
      "mnemo-memory": {
        "command": "python",
        "args": ["-m", "mnemo.mcp.mcp_server"],
        "env": {
          "MNEMO_BASE_URL": "http://localhost:8000",
          "MNEMO_API_KEY": "mnemo_..."
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
MNEMO_API_KEY = os.environ.get("MNEMO_API_KEY", "")
MNEMO_AGENT_ID = os.environ.get("MNEMO_AGENT_ID", "")  # deprecated fallback
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
        if MNEMO_API_KEY:
            _client = MnemoClient(MNEMO_BASE_URL, api_key=MNEMO_API_KEY)
            agent_info = await _client.me()
            _agent_id = UUID(agent_info["agent_id"] if "agent_id" in agent_info else agent_info["id"])
            logger.info("Authenticated as %s (%s)", agent_info.get("name"), _agent_id)
        else:
            _client = MnemoClient(MNEMO_BASE_URL)
            _agent_id = await _resolve_agent(_client)
            logger.info("Running without auth (set MNEMO_API_KEY for production)")
    return _client, _agent_id


async def _resolve_target_agent(
    client: MnemoClient, agent_id_str: str | None, default_id: UUID
) -> tuple[UUID | None, str | None]:
    """Return (target_uuid, None) on success, or (None, error_string) on failure.

    If agent_id_str is None, returns the default agent ID.
    If agent_id_str is provided, validates it's a UUID and that the agent exists.
    Non-default agents are never auto-registered — they must be pre-created via REST.
    """
    if agent_id_str is None:
        return default_id, None
    try:
        target_id = UUID(agent_id_str)
    except ValueError:
        return None, f"Invalid agent_id format: '{agent_id_str}'. Expected a UUID."
    try:
        await client.get_agent(target_id)
        return target_id, None
    except Exception:
        return None, (
            f"Agent {agent_id_str} not found. Register it first via the REST API."
        )


async def _resolve_agent(client: MnemoClient) -> UUID:
    """Return the configured agent's UUID, finding or creating by name.

    Resolution order:
    1. If MNEMO_AGENT_ID is set, verify it exists and use it (explicit override).
    2. Look up active agents named MNEMO_AGENT_NAME — reuse the first match.
    3. If no match, register a new agent with that name.

    This makes the MCP server idempotent across restarts: the stable identity
    is MNEMO_AGENT_NAME, not a hardcoded UUID.
    """
    if MNEMO_AGENT_ID:
        agent_id = UUID(MNEMO_AGENT_ID)
        try:
            await client.get_agent(agent_id)
            logger.info("Using explicitly configured agent %s", agent_id)
            return agent_id
        except Exception:
            logger.warning("Configured MNEMO_AGENT_ID %s not found, falling back to name lookup", agent_id)

    existing = await client.find_agent_by_name(MNEMO_AGENT_NAME)
    if existing:
        agent_id = UUID(existing[0]["id"])
        logger.info("Reconnected to existing agent '%s' → %s", MNEMO_AGENT_NAME, agent_id)
        return agent_id

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
        "Store a memory. By default stores for this agent. Pass agent_id to store for a "
        "different registered agent. Mnemo handles classification (episodic/semantic/procedural), "
        "confidence estimation, and graph linking automatically."
    ),
)
async def mnemo_remember(
    text: str,
    domain_tags: list[str] | None = None,
    agent_id: str | None = None,
) -> str:
    """
    Args:
        text: What to remember. Be specific — include context, outcomes, and lessons
              learned. Multi-sentence input is decomposed into typed atoms.
        domain_tags: Optional topic tags to organise memories (e.g. ["python", "debugging"]).
        agent_id: Optional UUID of a registered agent. Omit to use the default agent.
    """
    client, default_id = await _get_client()
    target_id, error = await _resolve_target_agent(client, agent_id, default_id)
    if error:
        return error
    result = await client.remember(
        agent_id=target_id,
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
        "Search memories. Returns first-sentence summaries by default. "
        "Set verbosity='full' for complete content. "
        "Pass agent_id to search a different agent's memories."
    ),
)
async def mnemo_recall(
    query: str,
    domain_tags: list[str] | None = None,
    max_results: int = 5,
    min_similarity: float = 0.15,
    similarity_drop_threshold: float | None = 0.3,
    verbosity: str = "summary",
    max_total_tokens: int | None = 500,
    agent_id: str | None = None,
) -> str:
    """
    Args:
        query: What you're looking for. Descriptive phrases work better than keywords —
               e.g. "how to handle CSV type coercion in pandas" not just "pandas".
        domain_tags: Optional filter to specific domains.
        max_results: Maximum number of primary results to return (default 5).
        min_similarity: Minimum cosine similarity to query (default 0.15).
        similarity_drop_threshold: Stop at relevance cliffs (default 0.3). Set None to disable.
        verbosity: "summary" (first sentence, default), "full" (complete), or "truncated".
        max_total_tokens: Approximate token budget for all returned content (default 500).
        agent_id: Optional UUID of a registered agent. Omit to search the default agent.
    """
    client, default_id = await _get_client()
    target_id, error = await _resolve_target_agent(client, agent_id, default_id)
    if error:
        return error
    result = await client.recall(
        agent_id=target_id,
        query=query,
        domain_tags=domain_tags,
        max_results=max_results,
        min_confidence=0.1,
        min_similarity=min_similarity,
        similarity_drop_threshold=similarity_drop_threshold,
        verbosity=verbosity,
        max_total_tokens=max_total_tokens,
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
    description=(
        "View memory statistics: total atoms, active count, confidence, and graph density. "
        "Pass agent_id to view stats for a different agent."
    ),
)
async def mnemo_stats(agent_id: str | None = None) -> str:
    """Returns a summary of the agent's memory state."""
    client, default_id = await _get_client()
    target_id, error = await _resolve_target_agent(client, agent_id, default_id)
    if error:
        return error
    s = await client.stats(agent_id=target_id)
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
