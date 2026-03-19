# Mnemo v0.2 — Implementation Specification

## For: Claude Code / Implementation Agent
## Status: READY TO BUILD
## Target: Working prototype on home server in ~1 week
## Revision: v0.2 — incorporates design review feedback

---

## Design Principles (READ FIRST)

These principles override any specific implementation detail below:

1. **Simple interface, rich internals.** The agent says what happened. The
   server does the remembering. Agents should never need to classify, label,
   or structure their own memories.

2. **Confidence is inferred, not declared.** The server estimates confidence
   from linguistic cues and source type. Agents never specify Beta distribution
   parameters.

3. **Decay is real, not decorative.** Every retrieval query uses effective
   confidence. Every retrieval updates access timestamps. The consolidation
   job actually runs on a schedule.

4. **Views are safe by construction.** Graph expansion within a shared view
   never returns atoms outside the view's filter scope.

5. **Snapshots only for v0.1.** No live subscriptions. Simpler, safer, and
   sufficient to prove the sharing value proposition.

6. **Departure revokes everything.** When an agent leaves, all capabilities
   it granted are cascade-revoked. Default is safe; `survive_departure` flag
   is a future enhancement.

---

## Overview

Build a minimal but extensible agent memory server called **Mnemo**. The
server provides persistent, typed memory storage for AI agents with semantic
retrieval, knowledge graph relationships, and basic view sharing (α=1 skill
export only for v0.1).

The system consists of:
1. **PostgreSQL database** with pgvector extension
2. **FastAPI server** exposing a REST API
3. **Decomposition service** that breaks free-text into typed atoms
4. **Python client library** for agent integration
5. **MCP server wrapper** for framework-agnostic discovery
Tech stack: Python 3.11+, FastAPI, asyncpg, pgvector, sentence-transformers
(for embeddings), PostgreSQL 16+. Use `uv` for package management.

---

## 1. Database Setup

### 1.1 Prerequisites

```bash
# Install PostgreSQL 16 with pgvector
sudo apt install postgresql-16
sudo apt install postgresql-16-pgvector

# Create database
sudo -u postgres createdb mnemo
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
```

### 1.2 Complete Schema

```sql
-- ============================================================
-- MNEMO v0.2 SCHEMA
-- ============================================================

-- Agent registry
CREATE TABLE agents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    persona         TEXT,
    domain_tags     TEXT[] NOT NULL DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    -- Departure handling
    departed_at     TIMESTAMPTZ,             -- NULL = active, set on departure
    data_expires_at TIMESTAMPTZ              -- departed_at + 30 days
);

CREATE INDEX idx_agents_domain_tags ON agents USING GIN (domain_tags);
CREATE INDEX idx_agents_active ON agents (is_active) WHERE is_active = true;

-- Core memory atoms
CREATE TABLE atoms (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,

    -- Type system (assigned by server, not by agent)
    atom_type       TEXT NOT NULL CHECK (atom_type IN (
                        'episodic', 'semantic', 'procedural', 'relational'
                    )),

    -- Content
    text_content    TEXT NOT NULL,
    structured      JSONB DEFAULT '{}',
    embedding       vector(384),             -- all-MiniLM-L6-v2 for v0.1

    -- Confidence (inferred by server, stored as Beta distribution)
    confidence_alpha FLOAT NOT NULL DEFAULT 2.0,
    confidence_beta  FLOAT NOT NULL DEFAULT 2.0,

    -- Provenance
    source_type     TEXT NOT NULL DEFAULT 'direct_experience'
                    CHECK (source_type IN (
                        'direct_experience', 'inference', 'shared_view',
                        'imported_skill', 'consolidation'
                    )),
    source_ref      UUID,
    derivation      UUID[] DEFAULT '{}',

    -- Domain tagging
    domain_tags     TEXT[] NOT NULL DEFAULT '{}',

    -- Temporal
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed   TIMESTAMPTZ,
    access_count    INTEGER NOT NULL DEFAULT 0,

    -- Decay
    decay_type      TEXT NOT NULL DEFAULT 'exponential'
                    CHECK (decay_type IN ('exponential', 'linear', 'none')),
    decay_half_life_days FLOAT NOT NULL DEFAULT 30.0,

    -- Soft delete
    is_active       BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX idx_atoms_agent_id ON atoms (agent_id);
CREATE INDEX idx_atoms_agent_type ON atoms (agent_id, atom_type);
CREATE INDEX idx_atoms_domain_tags ON atoms USING GIN (domain_tags);
CREATE INDEX idx_atoms_embedding ON atoms USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX idx_atoms_active ON atoms (agent_id, is_active) WHERE is_active = true;

-- Knowledge graph edges
CREATE TABLE edges (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id       UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    target_id       UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    edge_type       TEXT NOT NULL CHECK (edge_type IN (
                        'supports', 'contradicts', 'depends_on',
                        'generalises', 'specialises', 'motivated_by',
                        'evidence_for', 'supersedes'
                    )),
    weight          FLOAT NOT NULL DEFAULT 1.0
                    CHECK (weight >= 0.0 AND weight <= 1.0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source_id, target_id, edge_type)
);

CREATE INDEX idx_edges_source ON edges (source_id);
CREATE INDEX idx_edges_target ON edges (target_id);

-- Views (snapshots only for v0.1)
CREATE TABLE views (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_agent_id  UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    alpha           FLOAT NOT NULL DEFAULT 1.0,
    atom_filter     JSONB NOT NULL,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_views_owner ON views (owner_agent_id);

-- Snapshot atom cache (frozen atoms at time of snapshot)
-- This ensures the snapshot is immutable even as source atoms decay
CREATE TABLE snapshot_atoms (
    view_id         UUID NOT NULL REFERENCES views(id) ON DELETE CASCADE,
    atom_id         UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    PRIMARY KEY (view_id, atom_id)
);

-- Capabilities (access control)
CREATE TABLE capabilities (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    view_id         UUID NOT NULL REFERENCES views(id) ON DELETE CASCADE,
    grantor_id      UUID NOT NULL REFERENCES agents(id),
    grantee_id      UUID NOT NULL REFERENCES agents(id),
    permissions     TEXT[] NOT NULL DEFAULT '{read}',
    revoked         BOOLEAN NOT NULL DEFAULT false,
    parent_cap_id   UUID REFERENCES capabilities(id),
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_capabilities_grantee ON capabilities (grantee_id, revoked);
CREATE INDEX idx_capabilities_view ON capabilities (view_id);

-- Access log (immutable audit trail)
CREATE TABLE access_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL,
    action          TEXT NOT NULL,
    target_id       UUID,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_access_log_agent ON access_log (agent_id, created_at);

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

-- Effective confidence after decay
CREATE OR REPLACE FUNCTION effective_confidence(
    conf_alpha FLOAT,
    conf_beta FLOAT,
    decay_type TEXT,
    decay_half_life_days FLOAT,
    created_at TIMESTAMPTZ,
    last_accessed TIMESTAMPTZ,
    access_count INTEGER
) RETURNS FLOAT AS $$
DECLARE
    age_days FLOAT;
    decay_factor FLOAT;
    base_confidence FLOAT;
    access_bonus FLOAT;
BEGIN
    base_confidence := conf_alpha / (conf_alpha + conf_beta);

    IF decay_type = 'none' THEN
        RETURN base_confidence;
    END IF;

    age_days := EXTRACT(EPOCH FROM (
        now() - COALESCE(last_accessed, created_at)
    )) / 86400.0;

    -- Frequently accessed memories decay slower
    access_bonus := LN(1 + access_count) * 0.1;

    IF decay_type = 'exponential' THEN
        decay_factor := POWER(0.5, age_days / (
            decay_half_life_days * (1.0 + access_bonus)
        ));
    ELSE
        decay_factor := GREATEST(0.0, 1.0 - (age_days / (
            decay_half_life_days * 2.0 * (1.0 + access_bonus)
        )));
    END IF;

    RETURN base_confidence * decay_factor;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Cascade revoke all capabilities granted by a departing agent
CREATE OR REPLACE FUNCTION revoke_agent_capabilities(departing_agent_id UUID)
RETURNS INTEGER AS $$
DECLARE
    revoked_count INTEGER;
BEGIN
    WITH RECURSIVE cap_tree AS (
        -- Direct capabilities granted by this agent
        SELECT id FROM capabilities
        WHERE grantor_id = departing_agent_id AND revoked = false
        UNION
        -- All descendants
        SELECT c.id FROM capabilities c
        JOIN cap_tree ct ON c.parent_cap_id = ct.id
        WHERE c.revoked = false
    )
    UPDATE capabilities SET revoked = true
    WHERE id IN (SELECT id FROM cap_tree);

    GET DIAGNOSTICS revoked_count = ROW_COUNT;
    RETURN revoked_count;
END;
$$ LANGUAGE plpgsql;
```

---

## 2. FastAPI Server

### 2.1 Project Structure

```
mnemo/
├── server/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, CORS
│   ├── config.py             # Settings via pydantic-settings
│   ├── database.py           # asyncpg connection pool
│   ├── models.py             # Pydantic request/response models
│   ├── embeddings.py         # Embedding generation service
│   ├── decomposer.py         # NEW: breaks free-text into typed atoms
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── agents.py         # Agent CRUD + departure
│   │   ├── memory.py         # NEW: /remember endpoint (primary)
│   │   ├── atoms.py          # Power-user explicit atom CRUD
│   │   ├── views.py          # Create view, export skill
│   │   └── capabilities.py   # Grant, revoke, audit
│   └── services/
│       ├── __init__.py
│       ├── atom_service.py   # Store, retrieve, deduplicate
│       ├── graph_service.py  # Graph expansion (scope-bounded)
│       ├── view_service.py   # Snapshot creation and skill export
│       └── consolidation.py  # Background: decay, cluster, merge dupes
├── client/
│   ├── __init__.py
│   └── mnemo_client.py       # Python client library
├── mcp/
│   ├── __init__.py
│   └── mcp_server.py         # MCP wrapper
├── skills/
│   ├── claude_skill.md       # SKILL.md for Claude Code agents
│   └── openclaw_skill.md     # Skill file for OpenClaw/Moltbook agents
├── tests/
│   ├── test_remember.py      # NEW: test decomposition pipeline
│   ├── test_atoms.py
│   ├── test_graph.py
│   ├── test_views.py
│   ├── test_decay.py         # NEW: test that decay affects retrieval
├── pyproject.toml
├── docker-compose.yml
└── README.md
```

### 2.2 Configuration

```python
# server/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgresql://mnemo:mnemo@localhost:5432/mnemo"
    embedding_model: str = "all-MiniLM-L6-v2"  # 384-dim, fast, local
    embedding_dim: int = 384
    max_retrieval_results: int = 50
    default_retrieval_limit: int = 10
    graph_expansion_max_depth: int = 3
    consolidation_interval_minutes: int = 60
    min_effective_confidence: float = 0.05     # below this, atom is deactivated
    duplicate_similarity_threshold: float = 0.90  # above this, merge not create

    # Default decay half-lives (days) — assigned by server based on atom_type
    decay_episodic: float = 14.0
    decay_semantic: float = 90.0
    decay_procedural: float = 180.0
    decay_relational: float = 90.0

    # Confidence inference defaults
    confidence_direct_experience: tuple = (8.0, 1.0)   # high confidence
    confidence_inference: tuple = (4.0, 2.0)            # moderate
    confidence_shared: tuple = (3.0, 2.0)               # inherit with caution
    confidence_uncertain: tuple = (2.0, 3.0)            # low

    # Agent departure
    departure_retention_days: int = 30

    class Config:
        env_prefix = "MNEMO_"

settings = Settings()
```

### 2.3 Pydantic Models

```python
# server/models.py
from pydantic import BaseModel, Field
from typing import Optional, Literal
from uuid import UUID
from datetime import datetime

# ── Agents ──

class AgentCreate(BaseModel):
    name: str
    persona: Optional[str] = None
    domain_tags: list[str] = []
    metadata: dict = {}

class AgentResponse(BaseModel):
    id: UUID
    name: str
    persona: Optional[str]
    domain_tags: list[str]
    metadata: dict
    created_at: datetime
    is_active: bool

# ── Remember (primary interface) ──

class RememberRequest(BaseModel):
    """The simple interface. Agent just says what happened."""
    text: str                                   # free-form text
    domain_tags: list[str] = []                 # optional domain hints

class RememberResponse(BaseModel):
    """What the server created from the free-text input."""
    atoms_created: int
    edges_created: int
    atoms: list['AtomResponse']
    duplicates_merged: int                      # atoms that matched existing knowledge

# ── Atoms (power-user interface) ──

class AtomCreate(BaseModel):
    """Explicit atom creation for operators who want control."""
    atom_type: Literal["episodic", "semantic", "procedural", "relational"]
    text_content: str
    structured: dict = {}
    confidence: Optional[Literal["high", "medium", "low", "uncertain"]] = None
    source_type: str = "direct_experience"
    source_ref: Optional[UUID] = None
    domain_tags: list[str] = []

class AtomResponse(BaseModel):
    id: UUID
    agent_id: UUID
    atom_type: str
    text_content: str
    structured: dict
    confidence_expected: float              # α / (α + β)
    confidence_effective: float             # after decay
    source_type: str
    domain_tags: list[str]
    created_at: datetime
    last_accessed: Optional[datetime]
    access_count: int
    is_active: bool
    # Note: confidence_alpha and confidence_beta are internal.
    # The API returns expected and effective values only.

# ── Retrieval ──

class RetrieveRequest(BaseModel):
    query: str
    atom_types: Optional[list[str]] = None
    domain_tags: Optional[list[str]] = None
    min_confidence: float = 0.1             # default filters out faded atoms
    max_results: int = 10
    expand_graph: bool = True
    expansion_depth: int = 2
    include_superseded: bool = False

class RetrieveResponse(BaseModel):
    atoms: list[AtomResponse]               # primary results
    expanded_atoms: list[AtomResponse]      # found via graph expansion
    total_retrieved: int

# ── Edges ──

class EdgeCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    edge_type: Literal["supports", "contradicts", "depends_on", "generalises",
                        "specialises", "motivated_by", "evidence_for", "supersedes"]
    weight: float = Field(default=1.0, ge=0.0, le=1.0)

class EdgeResponse(BaseModel):
    id: UUID
    source_id: UUID
    target_id: UUID
    edge_type: str
    weight: float

# ── Views ──

class ViewCreate(BaseModel):
    name: str
    description: Optional[str] = None
    atom_filter: dict                       # {"atom_types": [...], "domain_tags": [...]}

class ViewResponse(BaseModel):
    id: UUID
    owner_agent_id: UUID
    name: str
    description: Optional[str]
    alpha: float
    atom_filter: dict
    atom_count: int
    created_at: datetime

class SkillExport(BaseModel):
    """Rendered α=1 view as a skill package."""
    view_id: UUID
    name: str
    description: Optional[str]
    domain_tags: list[str]
    procedures: list[AtomResponse]
    supporting_facts: list[AtomResponse]
    metadata: dict
    rendered_markdown: str                  # SKILL.md format

# ── Capabilities ──

class GrantCreate(BaseModel):
    view_id: UUID
    grantee_id: UUID
    permissions: list[str] = ["read"]
    expires_at: Optional[datetime] = None

class CapabilityResponse(BaseModel):
    id: UUID
    view_id: UUID
    grantor_id: UUID
    grantee_id: UUID
    permissions: list[str]
    revoked: bool
    expires_at: Optional[datetime]
    created_at: datetime

# ── Stats ──

class AgentStats(BaseModel):
    agent_id: UUID
    total_atoms: int
    active_atoms: int
    atoms_by_type: dict[str, int]
    total_edges: int
    avg_effective_confidence: float
    active_views: int
    granted_capabilities: int
    received_capabilities: int
```

### 2.4 Core API Endpoints

```python
# ══════════════════════════════════════════════════════
# PRIMARY INTERFACE — what agents use via MCP
# ══════════════════════════════════════════════════════

# POST   /v1/agents/{agent_id}/remember           → Store a memory (free-text, server decomposes)
# POST   /v1/agents/{agent_id}/recall              → Retrieve relevant memories

# ══════════════════════════════════════════════════════
# POWER-USER INTERFACE — for operators who want control
# ══════════════════════════════════════════════════════

# POST   /v1/agents/{agent_id}/atoms               → Store an explicit typed atom
# GET    /v1/agents/{agent_id}/atoms/{atom_id}      → Get a specific atom
# POST   /v1/agents/{agent_id}/atoms/link           → Create an edge
# DELETE /v1/agents/{agent_id}/atoms/{atom_id}       → Soft-delete

# ══════════════════════════════════════════════════════
# VIEWS AND SHARING
# ══════════════════════════════════════════════════════

# POST   /v1/agents/{agent_id}/views               → Create a snapshot view
# GET    /v1/agents/{agent_id}/views                → List agent's views
# GET    /v1/agents/{agent_id}/views/{view_id}/export_skill → Export as skill
# POST   /v1/agents/{agent_id}/grant               → Grant access to another agent
# POST   /v1/capabilities/{cap_id}/revoke           → Revoke (cascades)
# GET    /v1/agents/{agent_id}/shared_views         → Views shared with this agent
# POST   /v1/agents/{agent_id}/shared_views/{view_id}/recall → Recall through shared view

# ══════════════════════════════════════════════════════
# MANAGEMENT
# ══════════════════════════════════════════════════════

# POST   /v1/agents                                → Register agent
# GET    /v1/agents/{agent_id}                      → Get agent info
# POST   /v1/agents/{agent_id}/depart               → Agent departure (cascade revoke)
# GET    /v1/agents/{agent_id}/stats                → Memory statistics
# POST   /v1/agents/{agent_id}/consolidate           → Trigger manual consolidation
# GET    /v1/health                                 → Health check
```

### 2.5 Key Service Implementations

#### Decomposition Service (NEW — the core innovation)

```python
# server/decomposer.py
"""
Breaks free-text input into typed memory atoms with inferred confidence.

The agent says: "pandas.read_csv silently coerces mixed-type columns.
I discovered this processing client_data.csv when row 847 had a string
in the account_id column. From now on I should always specify dtype."

The decomposer produces:
  - Episodic atom: "Discovered silent type coercion processing
    client_data.csv when row 847 had a string in account_id column"
    confidence: Beta(8, 1) — direct experience, high confidence
  - Semantic atom: "pandas.read_csv silently coerces mixed-type columns"
    confidence: Beta(4, 2) — inferred fact, moderate confidence
  - Procedural atom: "Always specify dtype explicitly when using read_csv"
    confidence: Beta(4, 2) — inferred procedure, moderate confidence
  - Edges: episodic --evidence_for--> semantic
           procedural --motivated_by--> semantic

v0.1 IMPLEMENTATION: Rule-based classifier.
v0.2+: LLM-based decomposition for higher accuracy.

CLASSIFICATION RULES (v0.1):

Episodic markers (assign as episodic):
  - Past tense first-person: "I found", "I discovered", "I encountered",
    "I noticed", "I observed", "I hit", "I ran into"
  - Temporal references: "today", "yesterday", "just now", "while working on"
  - Specific context: file names, row numbers, error messages, timestamps

Procedural markers (assign as procedural):
  - Imperative mood: "always", "never", "should", "must", "make sure"
  - Action verbs: "use", "avoid", "prefer", "check", "validate", "specify"
  - Pattern: "when X, do Y", "instead of X, use Y", "to prevent X, do Y"

Semantic (default for everything else):
  - General statements of fact
  - Descriptions of how things work
  - Observations without personal context

CONFIDENCE INFERENCE RULES (v0.1):

High confidence — Beta(8, 1):
  - Episodic atoms (direct observation)
  - Phrases: "I confirmed", "I verified", "I tested", "definitely"

Moderate confidence — Beta(4, 2):
  - Inferred facts and procedures
  - Default for most decomposed atoms

Low confidence — Beta(2, 2):
  - Hedging language: "I think", "maybe", "possibly", "might"
  - Uncertainty: "not sure", "unclear", "seems like"

Very low confidence — Beta(2, 4):
  - Strong uncertainty: "I don't know if", "it could be", "might be wrong"

DECOMPOSITION ALGORITHM:

1. Split input text into sentences.
2. Classify each sentence by type using marker patterns.
3. Assign confidence based on linguistic cues.
4. Group adjacent sentences of the same type into single atoms.
5. For each atom, check for duplicates (see duplicate detection below).
6. Create edges between atoms from the same /remember call:
   - episodic --evidence_for--> semantic (if both present)
   - procedural --motivated_by--> semantic (if both present)
   - episodic --evidence_for--> procedural (if no semantic present)
7. Return created atoms and edges.
"""

import re
from dataclasses import dataclass

@dataclass
class DecomposedAtom:
    text: str
    atom_type: str              # episodic, semantic, procedural
    confidence_alpha: float
    confidence_beta: float
    structured: dict

# Marker patterns
EPISODIC_PATTERNS = [
    r'\bI\s+(found|discovered|encountered|noticed|observed|hit|ran into|saw|tried)\b',
    r'\b(today|yesterday|just now|this morning|last night)\b',
    r'\b(while|when I was)\s+(working|processing|debugging|testing|deploying)\b',
]

PROCEDURAL_PATTERNS = [
    r'\b(always|never|should|must|make sure|be sure to)\b',
    r'\b(use|avoid|prefer|check|validate|specify|ensure)\b.*\b(instead|rather|before|after)\b',
    r'\b(when|if)\b.*\b(do|use|try|run|set|add)\b',
    r'\b(to prevent|to avoid|to fix|to handle)\b',
    r'\b(best practice|pro tip|rule of thumb)\b',
]

HIGH_CONFIDENCE_PATTERNS = [
    r'\b(confirmed|verified|tested|definitely|certainly|proven)\b',
]

LOW_CONFIDENCE_PATTERNS = [
    r'\b(I think|maybe|possibly|might|perhaps|seems? like|appears? to)\b',
    r'\b(not sure|unclear|uncertain|don\'t know)\b',
]

def decompose(text: str) -> list[DecomposedAtom]:
    """Break free-text into typed atoms with inferred confidence."""
    sentences = _split_sentences(text)
    atoms = []

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 10:  # skip fragments
            continue

        atom_type = _classify_type(sentence)
        alpha, beta = _infer_confidence(sentence, atom_type)

        # Extract structured data (code snippets, etc.)
        structured = _extract_structured(sentence)

        atoms.append(DecomposedAtom(
            text=sentence,
            atom_type=atom_type,
            confidence_alpha=alpha,
            confidence_beta=beta,
            structured=structured,
        ))

    # Merge adjacent atoms of the same type
    atoms = _merge_adjacent(atoms)

    return atoms

def _split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries, preserving code blocks."""
    # Simple split on . ! ? followed by space or end
    # Preserve code blocks (backtick-delimited) as single units
    return re.split(r'(?<=[.!?])\s+', text)

def _classify_type(sentence: str) -> str:
    for pattern in EPISODIC_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return 'episodic'
    for pattern in PROCEDURAL_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return 'procedural'
    return 'semantic'

def _infer_confidence(sentence: str, atom_type: str) -> tuple[float, float]:
    # Check for explicit confidence markers first
    for pattern in LOW_CONFIDENCE_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return (2.0, 3.0)
    for pattern in HIGH_CONFIDENCE_PATTERNS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return (8.0, 1.0)

    # Default by type
    if atom_type == 'episodic':
        return (8.0, 1.0)   # direct experience = high confidence
    elif atom_type == 'procedural':
        return (4.0, 2.0)   # inferred procedure = moderate
    else:
        return (4.0, 2.0)   # inferred fact = moderate

def _extract_structured(sentence: str) -> dict:
    """Extract code snippets or patterns from text."""
    code_match = re.search(r'`([^`]+)`', sentence)
    if code_match:
        return {"code": code_match.group(1)}
    return {}

def _merge_adjacent(atoms: list[DecomposedAtom]) -> list[DecomposedAtom]:
    """Merge adjacent atoms of the same type into single atoms."""
    if not atoms:
        return []
    merged = [atoms[0]]
    for atom in atoms[1:]:
        if atom.atom_type == merged[-1].atom_type:
            merged[-1] = DecomposedAtom(
                text=merged[-1].text + " " + atom.text,
                atom_type=atom.atom_type,
                confidence_alpha=max(merged[-1].confidence_alpha, atom.confidence_alpha),
                confidence_beta=min(merged[-1].confidence_beta, atom.confidence_beta),
                structured={**merged[-1].structured, **atom.structured},
            )
        else:
            merged.append(atom)
    return merged
```

#### Atom Service (updated: decay in retrieval, duplicate detection)

```python
# server/services/atom_service.py
"""
Core business logic for storing and retrieving atoms.

STORE FLOW (via /remember):
1. Decomposer breaks text into typed atoms
2. For each atom:
   a. Generate embedding
   b. Check for duplicates (cosine similarity > 0.90 with same agent, same type)
   c. If duplicate found: increment existing atom's confidence (Bayesian update)
      α_new = α_existing + α_incoming - 1 (add evidence, subtract prior)
      Update last_accessed. Return existing atom. DO NOT create new atom.
   d. If no duplicate: insert new atom with server-assigned decay half-life
3. Create edges between atoms from the same /remember call
4. Log the access

RETRIEVE FLOW (via /recall):
1. Generate embedding from query text
2. Vector similarity search WITH effective_confidence in the query:

   SELECT *,
          1 - (embedding <=> $query_embedding) as similarity,
          effective_confidence(
              confidence_alpha, confidence_beta,
              decay_type, decay_half_life_days,
              created_at, last_accessed, access_count
          ) as eff_conf
   FROM atoms
   WHERE agent_id = $agent_id
     AND is_active = true
     AND ($atom_types IS NULL OR atom_type = ANY($atom_types))
     AND ($domain_tags IS NULL OR domain_tags && $domain_tags)
   ORDER BY similarity DESC
   LIMIT $max_results * 2      -- over-fetch to allow confidence filtering

   Then filter: WHERE eff_conf >= $min_confidence
   Then limit to $max_results

3. Filter out superseded atoms:
   Exclude atoms where EXISTS (
       SELECT 1 FROM edges e
       JOIN atoms a2 ON a2.id = e.source_id
       WHERE e.target_id = atom.id
         AND e.edge_type = 'supersedes'
         AND a2.is_active = true
   )

4. If expand_graph=True: follow edges (SCOPE-BOUNDED, see graph service)
5. UPDATE all returned atoms: last_accessed = now(), access_count = access_count + 1
6. Return primary + expanded results

DUPLICATE DETECTION:
   SELECT id, confidence_alpha, confidence_beta,
          1 - (embedding <=> $new_embedding) as similarity
   FROM atoms
   WHERE agent_id = $agent_id
     AND atom_type = $new_type
     AND is_active = true
     AND 1 - (embedding <=> $new_embedding) > 0.90
   ORDER BY similarity DESC
   LIMIT 1

   If found: merge by updating confidence, don't create new atom.
"""
```

#### Graph Service (updated: scope-bounded expansion)

```python
# server/services/graph_service.py
"""
Graph expansion via recursive CTEs.

CRITICAL: When expanding within a shared view, expansion MUST be bounded
by the view's atom filter. No edge can pull in an atom outside the granted
scope.

expand(atom_ids, edge_types, depth, scope_filter=None)

If scope_filter is None (agent's own retrieval): expand freely across
all of the agent's active atoms.

If scope_filter is provided (shared view retrieval): every expanded atom
must also match the view's filter. This prevents graph edges from leaking
atoms outside the view scope.

SQL pattern (scope-bounded):

    WITH RECURSIVE expanded AS (
        SELECT a.id, 0 as depth, 1.0 as relevance
        FROM atoms a
        WHERE a.id = ANY($seed_ids)

        UNION

        SELECT
            CASE WHEN e.source_id = ex.id THEN e.target_id
                 ELSE e.source_id END as id,
            ex.depth + 1 as depth,
            ex.relevance * e.weight * 0.7 as relevance
        FROM expanded ex
        JOIN edges e ON (e.source_id = ex.id OR e.target_id = ex.id)
        JOIN atoms a ON a.id = (
            CASE WHEN e.source_id = ex.id THEN e.target_id
                 ELSE e.source_id END
        )
        WHERE ex.depth < $max_depth
          AND ($edge_types IS NULL OR e.edge_type = ANY($edge_types))
          AND a.is_active = true
          -- SCOPE BOUNDARY: only expand to atoms matching the view filter
          AND ($scope_filter IS NULL
               OR (($scope_atom_types IS NULL OR a.atom_type = ANY($scope_atom_types))
                   AND ($scope_domain_tags IS NULL OR a.domain_tags && $scope_domain_tags)))
    )
    SELECT DISTINCT ON (a.id) a.*, ex.depth, ex.relevance
    FROM expanded ex
    JOIN atoms a ON a.id = ex.id
    WHERE a.is_active = true
    ORDER BY a.id, ex.relevance DESC
"""
```

#### View Service (updated: snapshots only, with frozen atom cache)

```python
# server/services/view_service.py
"""
View creation and skill export. v0.1: Snapshots only, α=1 only.

CREATE SNAPSHOT FLOW:
1. Validate filter
2. Query matching atoms at current time
3. Insert view record
4. Insert atom IDs into snapshot_atoms table (freezes the set)
5. The snapshot is now immutable — even if source atoms decay or are
   deleted, the snapshot retains the atom IDs it captured

EXPORT SKILL FLOW:
1. Load atoms from snapshot_atoms join atoms
2. Collect procedural atoms
3. Follow edges WITHIN the snapshot scope (scope-bounded expansion)
   to get supporting semantic atoms
4. Package as SkillExport with rendered markdown

SKILL MARKDOWN RENDERING:

    # {view.name}

    {view.description}

    ## Procedures

    ### {procedural_atom.text_content}
    *Confidence: {confidence_expected:.0%}*

    ```
    {structured.code if present}
    ```

    **Supporting knowledge:**
    - {linked semantic atom.text_content} ({confidence:.0%})

    ---

    *Generated by Mnemo on {timestamp}*
    *Source agent: {agent.name}*
    *Domain: {domain_tags}*

SHARED VIEW RETRIEVAL:
1. Validate capability (not revoked, not expired)
2. Run retrieval query but ONLY against atoms in snapshot_atoms for this view
3. Graph expansion is scope-bounded to snapshot atoms only
4. Update access_count on the capability for audit
"""
```

#### Consolidation Service (updated: no contradiction detection, duplicate merging)

```python
# server/services/consolidation.py
"""
Background consolidation job. Run periodically (default: every 60 min).
Register as a FastAPI lifespan background task.

CONSOLIDATION STEPS:

1. DECAY: Deactivate faded atoms
   UPDATE atoms SET is_active = false
   WHERE is_active = true
     AND effective_confidence(
         confidence_alpha, confidence_beta,
         decay_type, decay_half_life_days,
         created_at, last_accessed, access_count
     ) < 0.05;
   Log count of deactivated atoms.

2. CLUSTER: Find groups of 3+ episodic atoms with cosine similarity > 0.85
   within the same agent and overlapping domain tags.

3. GENERALISE: For each cluster:
   - Compute centroid embedding
   - Text: "Generalised from {n} observations: {most confident atom text}"
   - Confidence: Beta(sum of alphas, max of betas)
   - Type: semantic
   - Create 'generalises' edges from new atom to each episodic atom
   - Source_type: 'consolidation'

4. MERGE DUPLICATES: Find atom pairs with similarity > 0.90 within same
   agent, same type, same domain overlap.
   - Keep the older atom (preserves provenance)
   - Add the newer atom's evidence: α_old += α_new - 1, β_old += β_new - 1
   - Reassign edges from the newer atom to the older atom
   - Deactivate the newer atom
   - Create 'generalises' edge from older to newer (audit trail)
   Log count of merged atoms.

5. DEPARTED AGENT CLEANUP: Delete atoms and views for agents where
   data_expires_at < now().

6. LOG: Record consolidation run metadata in access_log:
   {action: 'consolidation', metadata: {decayed: N, clustered: N, merged: N, purged: N}}

SCHEDULING:
    Register in FastAPI lifespan:

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(consolidation_loop())
        yield
        task.cancel()

    async def consolidation_loop():
        while True:
            await asyncio.sleep(settings.consolidation_interval_minutes * 60)
            await run_consolidation()
"""
```

#### Agent Departure Service (NEW)

```python
# In server/routes/agents.py

"""
POST /v1/agents/{agent_id}/depart

Agent departure flow:
1. Validate agent exists and is active
2. Cascade revoke all capabilities this agent has granted
   (uses revoke_agent_capabilities SQL function)
3. Set agent.is_active = false
4. Set agent.departed_at = now()
5. Set agent.data_expires_at = now() + 30 days
6. Log the departure in access_log
7. Return summary: {capabilities_revoked: N, data_expires_at: timestamp}

The agent's atoms, views, and edges are preserved for 30 days so the
operator can export data. After data_expires_at, the consolidation job
purges everything.

Capabilities the agent RECEIVED are not affected — those are owned by
the granting agent and remain valid.
"""
```

---

## 3. Client Library

```python
# client/mnemo_client.py
"""
Lightweight async Python client for the Mnemo API.

Usage:
    client = MnemoClient("http://localhost:8000")

    # Register an agent
    agent = await client.register_agent("my-agent",
        persona="python developer", domain_tags=["python"])

    # Remember something (simple interface)
    result = await client.remember(
        agent_id=agent["id"],
        text="pandas.read_csv silently coerces mixed-type columns. "
             "I discovered this processing client_data.csv. "
             "From now on I should always specify dtype explicitly.",
        domain_tags=["python", "pandas"]
    )
    # result: {atoms_created: 3, edges_created: 2, duplicates_merged: 0}

    # Recall relevant memories
    results = await client.recall(
        agent_id=agent["id"],
        query="loading CSV files with pandas",
        max_results=10
    )

    # Create and share a skill
    view = await client.create_view(
        agent_id=agent["id"],
        name="pandas-csv-handling",
        atom_filter={"atom_types": ["procedural"], "domain_tags": ["pandas"]},
    )

    skill = await client.export_skill(agent_id=agent["id"], view_id=view["id"])
    # skill includes rendered_markdown: "# pandas-csv-handling\n..."

    cap = await client.grant(
        agent_id=agent["id"],
        view_id=view["id"],
        grantee_id=other_agent["id"]
    )
"""

import httpx
from typing import Optional
from uuid import UUID

class MnemoClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    # ── Primary Interface ──

    async def remember(self, agent_id: UUID, text: str,
                        domain_tags: list[str] = None) -> dict:
        """Store a memory. Just say what happened. Server does the rest."""
        resp = await self.http.post(f"/v1/agents/{agent_id}/remember", json={
            "text": text,
            "domain_tags": domain_tags or [],
        })
        resp.raise_for_status()
        return resp.json()

    async def recall(self, agent_id: UUID, query: str,
                      atom_types: list[str] = None,
                      domain_tags: list[str] = None,
                      min_confidence: float = 0.1,
                      max_results: int = 10,
                      expand_graph: bool = True) -> dict:
        """Retrieve relevant memories by semantic search."""
        resp = await self.http.post(f"/v1/agents/{agent_id}/recall", json={
            "query": query,
            "atom_types": atom_types,
            "domain_tags": domain_tags,
            "min_confidence": min_confidence,
            "max_results": max_results,
            "expand_graph": expand_graph,
        })
        resp.raise_for_status()
        return resp.json()

    # ── Agent Management ──

    async def register_agent(self, name: str, persona: str = None,
                              domain_tags: list[str] = None) -> dict:
        resp = await self.http.post("/v1/agents", json={
            "name": name, "persona": persona, "domain_tags": domain_tags or []
        })
        resp.raise_for_status()
        return resp.json()

    async def depart(self, agent_id: UUID) -> dict:
        """Initiate agent departure. Cascade revokes all granted capabilities."""
        resp = await self.http.post(f"/v1/agents/{agent_id}/depart")
        resp.raise_for_status()
        return resp.json()

    # ── Power-User Atom Operations ──

    async def store_atom(self, agent_id: UUID, atom_type: str,
                          text_content: str, structured: dict = None,
                          confidence: str = None,
                          domain_tags: list[str] = None) -> dict:
        """Explicit atom creation (power-user interface)."""
        resp = await self.http.post(f"/v1/agents/{agent_id}/atoms", json={
            "atom_type": atom_type,
            "text_content": text_content,
            "structured": structured or {},
            "confidence": confidence,
            "domain_tags": domain_tags or [],
        })
        resp.raise_for_status()
        return resp.json()

    async def link(self, source_id: UUID, target_id: UUID,
                    edge_type: str, weight: float = 1.0) -> dict:
        """Create a typed edge between two atoms."""
        resp = await self.http.post("/v1/atoms/link", json={
            "source_id": str(source_id),
            "target_id": str(target_id),
            "edge_type": edge_type,
            "weight": weight,
        })
        resp.raise_for_status()
        return resp.json()

    # ── View Operations ──

    async def create_view(self, agent_id: UUID, name: str,
                           atom_filter: dict, description: str = None) -> dict:
        resp = await self.http.post(f"/v1/agents/{agent_id}/views", json={
            "name": name, "description": description,
            "atom_filter": atom_filter,
        })
        resp.raise_for_status()
        return resp.json()

    async def export_skill(self, agent_id: UUID, view_id: UUID) -> dict:
        resp = await self.http.get(
            f"/v1/agents/{agent_id}/views/{view_id}/export_skill")
        resp.raise_for_status()
        return resp.json()

    async def grant(self, agent_id: UUID, view_id: UUID,
                     grantee_id: UUID) -> dict:
        resp = await self.http.post(f"/v1/agents/{agent_id}/grant", json={
            "view_id": str(view_id),
            "grantee_id": str(grantee_id),
            "permissions": ["read"],
        })
        resp.raise_for_status()
        return resp.json()

    async def revoke(self, capability_id: UUID) -> dict:
        resp = await self.http.post(f"/v1/capabilities/{capability_id}/revoke")
        resp.raise_for_status()
        return resp.json()

    async def recall_shared(self, agent_id: UUID, view_id: UUID,
                             query: str, max_results: int = 10) -> dict:
        """Recall through a shared view (scope-bounded)."""
        resp = await self.http.post(
            f"/v1/agents/{agent_id}/shared_views/{view_id}/recall",
            json={"query": query, "max_results": max_results}
        )
        resp.raise_for_status()
        return resp.json()

    # ── Stats ──

    async def stats(self, agent_id: UUID) -> dict:
        resp = await self.http.get(f"/v1/agents/{agent_id}/stats")
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.http.aclose()
```

---

## 4. Skill Files (Deliverable)

### 5.1 Claude Code Skill (skills/claude_skill.md)

Write AFTER the API is stable. Template:

```markdown
# Mnemo Memory — Claude Code Integration

## What This Does
Mnemo provides persistent memory across sessions. You can remember things
and recall them later, share knowledge with other agents as skills, and
build up expertise over time.

## Quick Start
{how to configure the MCP server URL and API key}

## Tools

### remember
Store a memory. Just describe what happened, what you learned, or what
you'd do differently. Mnemo handles classification and linking.
{parameters and examples}

### recall
Search your memories. Describe what you're looking for in natural language.
{parameters and examples}

### export_skill
Package your procedural knowledge as a shareable skill.
{parameters and examples}

## Tips
- Remember things right after you learn them — don't wait.
- Be specific: "pd.read_csv coerces mixed types" is better than "pandas
  has issues".
- Include context: "while processing client_data.csv" helps future recall.
- Domain tags help: tag memories so they're findable by topic.
```

### 5.2 OpenClaw / Moltbook Skill (skills/openclaw_skill.md)

Format TBD pending OpenClaw skill specification. Same content, different
packaging.

---

## 6. Docker Compose, Dockerfile, pyproject.toml

Same as v0.1 with one addition to pyproject.toml:

```toml
[project]
name = "mnemo"
version = "0.2.0"
description = "Persistent, Permissioned, Portable Memory for AI Agents"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.109.0",
    "uvicorn[standard]>=0.27.0",
    "asyncpg>=0.29.0",
    "pgvector>=0.2.4",
    "sentence-transformers>=2.3.0",
    "pydantic>=2.5.0",
    "pydantic-settings>=2.1.0",
    "httpx>=0.26.0",
    "numpy>=1.26.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.23.0",
    "httpx>=0.26.0",
]

[tool.uv]
# Tom uses uv for package management
```

---

## 7. What to Build First (Priority Order)

1. **Database + schema** (30 min with Docker Compose)
2. **Embedding service** (20 min)
3. **Decomposer** (2 hours — rule-based classifier + confidence inference)
4. **Agent CRUD + departure** (45 min)
5. **`/remember` endpoint** (2 hours — decompose + deduplicate + store + link)
6. **`/recall` endpoint** (2 hours — semantic search + decay filtering + access update)
7. **Graph expansion** (1 hour — scope-bounded recursive CTE)
8. **Stats endpoint** (30 min)
9. **Client library** (1 hour)
11. **View creation + snapshot caching** (1 hour)
12. **Skill export with markdown rendering** (1 hour)
13. **Capabilities + grant/revoke + cascade** (1-2 hours)
14. **Consolidation job** (2 hours — decay + cluster + merge dupes + purge)
15. **Skill files** (1 hour — Claude + OpenClaw, write last)

**Total estimated: ~18 hours of focused work, or a solid weekend + one evening with Claude Code.**

---

## 8. What to Measure

- **Retrieval hit rate over time**: improves as memory accumulates?
- **Knowledge transfer**: receiving agent's hit rate improves after skill import?
- **Decomposition accuracy**: do the type classifications feel right? (Manual spot-check)
- **Duplicate detection**: are near-duplicate memories being merged correctly?
- **Decay dynamics**: do unaccessed atoms actually fade from retrieval results?
- **Graph density**: ~2-4 edges per atom?
- **Consolidation**: episodic → semantic generalisation happening?
- **Skill export quality**: exported markdown is coherent and useful?
- **Scope safety**: shared view retrieval never returns atoms outside filter scope?

---

## 9. What NOT to Build Yet

- α ≠ 1 projections (geometric framework beyond skill export)
- Live subscriptions (snapshots only for v0.1)
- Push notifications / webhooks
- LEAN4 verification
- Cross-platform embedding alignment
- LLM-based decomposition (rule-based is fine for v0.1)
- Contradiction detection (duplicate merging handles the common case)
- Authentication / API keys (use agent_id as implicit auth for prototype)
- Rate limiting
- Horizontal scaling
- Any UI
- `survive_departure` flag on shared snapshots

---

## 10. Changelog from v0.1

| Change | Rationale |
|--------|-----------|
| Added `/remember` endpoint (primary interface) | Agents shouldn't classify their own memories |
| Added decomposer service | Server breaks free-text into typed atoms |
| Confidence inferred by server | LLMs can't meaningfully output Beta parameters |
| Power-user `/atoms` endpoint retained | Operators who want control still have it |
| Renamed `/retrieve` to `/recall` | Consistent naming (remember/recall) |
| Added duplicate detection + merge | Replaces contradiction detection; simpler, more useful |
| Removed contradiction detection | Hard NLP problem; duplicate merging handles common case |
| Removed live subscriptions | Snapshots only for v0.1; simpler, avoids fading/scope issues |
| Added snapshot_atoms table | Freezes snapshot contents, immune to source atom decay |
| Added scope-bounded graph expansion | Shared view retrieval cannot leak atoms outside filter scope |
| Added agent departure flow | Cascade revoke all granted capabilities; 30-day data retention |
| Added revoke_agent_capabilities SQL function | Efficient cascade revocation |
| Decay wired into retrieval query | effective_confidence used in WHERE/ORDER BY |
| Retrieval updates access timestamps | last_accessed and access_count updated on every retrieval |
| Consolidation includes departed agent purge | Cleans up expired data |
| Added skill files as deliverable | Claude Code and OpenClaw skill documentation |
| All endpoints use /v1/ prefix | API versioning from day one |
| AtomResponse hides Beta parameters | Returns expected + effective confidence, not α/β |
| Explicit atom confidence uses labels | "high"/"medium"/"low"/"uncertain" not floats |
| uv for package management | Tom's preference |
```

