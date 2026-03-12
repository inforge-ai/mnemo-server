from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://mnemo:mnemo@localhost:5432/mnemo"
    embedding_model: str = "thenlper/gte-small"
    embedding_dim: int = 384
    max_retrieval_results: int = 50
    default_retrieval_limit: int = 10
    graph_expansion_max_depth: int = 3
    consolidation_interval_minutes: int = 60
    min_effective_confidence: float = 0.05
    duplicate_similarity_threshold: float = 0.90

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

    # Auth
    auth_enabled: bool = False

    # Admin
    admin_token: str = ""  # set MNEMO_ADMIN_TOKEN; empty = admin disabled

    # Testing
    sync_store_for_tests: bool = False  # if True, /remember awaits the store task inline

    class Config:
        env_prefix = "MNEMO_"


settings = Settings()
