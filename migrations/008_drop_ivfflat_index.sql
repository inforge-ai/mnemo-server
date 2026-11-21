-- Migration 008: drop the ivfflat embedding index (v2 Phase 0)
--
-- The ivfflat index (lists=100, probes=10) scans ~10% of lists across all
-- agents and then applies the per-agent filter, so recall for smaller agents
-- could return fewer rows than requested. With ~13k production atoms an exact
-- scan over the agent's own rows is fast and always correct. Revisit an ANN
-- index (HNSW with iterative scans) when volume warrants it.

DROP INDEX IF EXISTS idx_atoms_embedding;
