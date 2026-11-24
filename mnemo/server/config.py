import os
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str  # required — set MNEMO_DATABASE_URL in env or .env file
    embedding_model: str = "google/embeddinggemma-300m"
    # "fake" is a deterministic hashed bag-of-words embedder for the unit
    # test suite (no model download, no network). Never use it in production.
    embedding_backend: Literal["sentence_transformers", "fake"] = "sentence_transformers"
    # Decomposer v2 (docs/phase1-design.md §4). No silent fallback: the
    # server refuses to start with "anthropic" and no ANTHROPIC_API_KEY.
    # "regex" only when chosen explicitly; "fake" = scripted test decomposer.
    # "openrouter" is for the bake-off against mnemo_eval / test DBs only.
    decomposer_provider: Literal["anthropic", "openrouter", "regex", "fake"] = "anthropic"
    decomposer_prompt: Literal["v2", "v3"] = "v3"   # v3: event_date + date_precision, no confidence (Phase 2)
    decomposer_model: str = "claude-haiku-4-5-20251001"
    decomposer_deadline_seconds: float = 90.0  # whole decomposition, attempts included
    allow_non_anthropic_decomposer: bool = False
    embedding_dim: int = 768
    max_retrieval_results: int = 50
    default_retrieval_limit: int = 10
    graph_expansion_max_depth: int = 3
    consolidation_interval_minutes: int = 60
    min_effective_confidence: float = 0.05

    # v2 Phase 0: destructive consolidation steps are OFF by default. Each
    # step soft-deletes or rewrites memories (decay deactivation, generalised
    # copies, newer-of-pair merges, edge deletion); enable explicitly only
    # when the data loss is intended. Departed-agent purge is unaffected.
    # WARNING: do not enable before Phase 3. Phase 0 restored ~3k atoms
    # whose decayed confidence is below min_effective_confidence; re-enabling
    # decay would deactivate all of them again on the next run.
    consolidation_decay_enabled: bool = False
    consolidation_generalise_enabled: bool = False
    consolidation_merge_enabled: bool = False
    consolidation_prune_edges_enabled: bool = False

    # v2 Phase 0: when False, recall ignores the min_confidence floor (clients
    # send 0.1 explicitly, which hides restored and older atoms). Confidence
    # still affects ranking through composite_score.
    recall_confidence_floor_enabled: bool = False

    # v2 Phase 0: refuse to start when migrations/*.sql has unapplied files.
    # Apply them with `mnemo migrate`.
    require_migrations: bool = True
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

    # Lifecycle relationship detection (docs/episodic_suppression-tension.md).
    # Detection band [low, high) excludes off-topic pairs and dedup-band pairs.
    # Asymmetric thresholds: supersedes is destructive (silently retires an
    # atom), so it requires more LLM confidence than the additive types.
    lifecycle_detection_enabled: bool = False
    lifecycle_band_low: float = 0.50
    lifecycle_band_high: float = 0.90
    lifecycle_candidate_limit: int = 5
    lifecycle_llm_timeout_seconds: float = 5.0
    lifecycle_max_concurrency: int = 4  # pair classifications in flight at once, per new atom
    supersedes_threshold: float = 0.75
    tension_threshold: float = 0.65
    narrows_threshold: float = 0.65
    # Recall and recent hide atoms that are the target of a supersedes edge from an
    # active atom. False = return them (as if every request set include_superseded).
    recall_hide_superseded: bool = True
    # v2 Phase 2 decision models (docs/phase2-design.md §5). Every OpenRouter
    # request carries provider {"zdr": true}. The production key lives in the
    # production .env (MNEMO_OPENROUTER_API_KEY, a dedicated key with a cap).
    decision_provider: Literal["off", "openrouter", "fake"] = "off"
    decision_model: str = "typesafe/jev-1.14"
    decision_fallback_model: str = "liquid/d1.2-ultra"
    decision_request_timeout_seconds: float = 1.5
    decision_retries: int = 2
    openrouter_api_key: str | None = None
    # Logging
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _test_backends_need_test_db(self):
        """Refuse test-only backends outside a test database.

        The fake embedder and fake decomposer would silently store garbage
        vectors / regex atoms; they are only allowed when MNEMO_DATABASE_URL
        names a test database. Runs at Settings() construction, so the
        server, CLI and scripts all refuse to start.
        """
        fakes = []
        if self.embedding_backend == "fake":
            fakes.append("MNEMO_EMBEDDING_BACKEND=fake")
        if self.decomposer_provider == "fake":
            fakes.append("MNEMO_DECOMPOSER_PROVIDER=fake")
        if self.decision_provider == "fake":
            fakes.append("MNEMO_DECISION_PROVIDER=fake")
        if fakes and "test" not in self.database_url:
            raise ValueError(
                f"{', '.join(fakes)} is only allowed when MNEMO_DATABASE_URL "
                "points at a test database (its URL must contain 'test')."
            )
        # Spec rule 4: no production memory text to a non-Anthropic provider.
        if self.decomposer_provider == "openrouter" and not (
            self.allow_non_anthropic_decomposer
            and ("mnemo_eval" in self.database_url or "test" in self.database_url)
        ):
            raise ValueError(
                "MNEMO_DECOMPOSER_PROVIDER=openrouter requires "
                "MNEMO_ALLOW_NON_ANTHROPIC_DECOMPOSER=true and a mnemo_eval or test database."
            )
        return self

    # MNEMO_ENV_FILE lets the test suite read .env.test instead of the
    # production .env.
    model_config = {
        "env_prefix": "MNEMO_",
        "env_file": os.environ.get("MNEMO_ENV_FILE", ".env"),
        "extra": "ignore",
    }


settings = Settings()
