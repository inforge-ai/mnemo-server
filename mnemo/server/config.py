from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str  # required — set MNEMO_DATABASE_URL in env or .env file
    test_database_url: str = ""  # set MNEMO_TEST_DATABASE_URL for test suite
    embedding_model: str = "google/embeddinggemma-300m"
    embedding_dim: int = 768
    max_retrieval_results: int = 50
    default_retrieval_limit: int = 10
    graph_expansion_max_depth: int = 3
    consolidation_interval_minutes: int = 60
    min_effective_confidence: float = 0.05
    duplicate_similarity_threshold: float = 0.90
    cross_call_edge_threshold: float = 0.55

    # Graph-aware recall (Ticket 2, Phase 1): after vector top-k, expand
    # 1-hop along edges and rescore as source_score * edge_weight * discount.
    # Ceiling caps graph matches at (multiplier × max_results) atoms.
    graph_recall_edge_discount: float = 0.5
    graph_recall_expansion_ceiling_multiplier: int = 2

    # Episodic recency ranking (Ticket 4b, Phase 2): within a recall, when
    # two episodic atoms are semantically near-duplicates (cosine sim >
    # episodic_recency_similarity_threshold) and one has a newer
    # remembered_on, the older atom's composite score is multiplied by
    # episodic_recency_demotion_factor. NULL remembered_on falls back to
    # created_at. Non-episodic atoms are never demoted by this mechanism.
    #
    # Similarity threshold calibration: the original 0.85 missed realistic
    # planned/completed pairs (the Zulip text from Ticket 4 sits at 0.816;
    # ABACAB/BAM/Sampo-style pairs cluster in [0.80, 0.86]). 0.80 catches
    # these while staying ~2.4x above the negative-control floor (unrelated
    # episodics at ~0.33). Empirical re-calibration against a human-reviewed
    # sample of prod episodic pairs is a follow-up ticket.
    episodic_recency_demotion_factor: float = 0.5
    episodic_recency_similarity_threshold: float = 0.80

    # Default decay half-lives (days) by atom type
    decay_episodic: float = 14.0
    decay_semantic: float = 90.0
    decay_procedural: float = 180.0
    decay_relational: float = 90.0

    # Confidence Beta(alpha, beta) defaults by source type
    confidence_direct_experience: tuple = (8.0, 1.0)
    confidence_inference: tuple = (4.0, 2.0)
    confidence_shared: tuple = (3.0, 2.0)
    confidence_uncertain: tuple = (2.0, 3.0)

    # Agent departure
    departure_retention_days: int = 30

    # Admin key (formerly admin_token) — accepts both MNEMO_ADMIN_KEY and MNEMO_ADMIN_TOKEN
    admin_key: str = Field(
        default="",
        validation_alias=AliasChoices("MNEMO_ADMIN_KEY", "MNEMO_ADMIN_TOKEN"),
    )

    # Testing
    sync_store_for_tests: bool = False  # if True, /remember awaits the store task inline

    model_config = {"env_prefix": "MNEMO_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
