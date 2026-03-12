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
    address: Optional[str] = None

# ── Remember (primary interface) ──

class RememberRequest(BaseModel):
    """The simple interface. Agent just says what happened."""
    text: str
    domain_tags: list[str] = []

class RememberResponse(BaseModel):
    atoms_created: int
    edges_created: int
    atoms: list["AtomResponse"]
    duplicates_merged: int

# ── Atoms (power-user interface) ──

class AtomCreate(BaseModel):
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
    confidence_expected: float
    confidence_effective: float
    relevance_score: Optional[float] = None
    source_type: str
    domain_tags: list[str]
    created_at: datetime
    last_accessed: Optional[datetime]
    access_count: int
    is_active: bool

# ── Retrieval ──

class RetrieveRequest(BaseModel):
    query: str
    atom_types: Optional[list[str]] = None
    domain_tags: Optional[list[str]] = None
    min_confidence: float = 0.1
    min_similarity: float = 0.2
    max_results: int = 10
    expand_graph: bool = True
    expansion_depth: int = 2
    include_superseded: bool = False
    similarity_drop_threshold: Optional[float] = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Stop returning results when score drops by this fraction from previous",
    )
    verbosity: str = Field(
        default="full",
        pattern="^(full|summary|truncated)$",
    )
    max_content_chars: int = Field(
        default=200,
        ge=50,
        le=5000,
        description="Character limit per atom when verbosity=truncated",
    )
    max_total_tokens: Optional[int] = Field(
        default=None,
        ge=50,
        le=10000,
        description="Approximate token budget for all returned content",
    )

class RetrieveResponse(BaseModel):
    atoms: list[AtomResponse]
    expanded_atoms: list[AtomResponse]
    total_retrieved: int

# ── Edges ──

class EdgeCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    edge_type: Literal[
        "supports", "contradicts", "depends_on", "generalises",
        "specialises", "motivated_by", "evidence_for", "supersedes", "summarises", "related"
    ]
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
    atom_filter: dict  # {"atom_types": [...], "domain_tags": [...]}

class ViewResponse(BaseModel):
    id: UUID
    owner_agent_id: UUID
    name: str
    description: Optional[str]
    alpha: float
    atom_filter: dict
    atom_count: int
    created_at: datetime

class SharedViewResponse(BaseModel):
    id: UUID
    owner_agent_id: UUID
    name: str
    description: Optional[str]
    alpha: float
    atom_filter: dict
    atom_count: int
    created_at: datetime
    grantor_id: Optional[UUID] = None
    source_address: Optional[str] = None
    granted_at: Optional[datetime] = None

class SharedRecallRequest(BaseModel):
    query: str
    from_agent: Optional[str] = None
    min_similarity: float = Field(default=0.15, ge=0.0, le=1.0)
    max_results: int = Field(default=5, ge=1, le=100)
    verbosity: str = Field(default="summary", pattern="^(full|summary|truncated)$")
    max_total_tokens: Optional[int] = Field(default=None, ge=50, le=10000)

class SkillExport(BaseModel):
    view_id: UUID
    name: str
    description: Optional[str]
    domain_tags: list[str]
    procedures: list[AtomResponse]
    supporting_facts: list[AtomResponse]
    metadata: dict
    rendered_markdown: str

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
    arc_atoms: int
    total_edges: int
    avg_effective_confidence: float
    active_views: int
    granted_capabilities: int
    received_capabilities: int
    address: Optional[str] = None
