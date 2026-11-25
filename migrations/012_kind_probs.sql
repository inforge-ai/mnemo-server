-- Migration 012: the full kind distribution per atom (v2 Phase 2, G3 prep; Tom, 2026-11-25)
--
-- kind_probs: {"state": p, "plan": p, "observation": p, "rule": p} from the kind
-- question. kind / kind_p stay the argmax (kind is 'unknown' below the kind
-- threshold) for display. The marginal decision variant (relations.decide_marginal)
-- sums over this distribution instead of thresholding it. NULL until asked.

ALTER TABLE atoms
    ADD COLUMN IF NOT EXISTS kind_probs JSONB
        CONSTRAINT atoms_kind_probs_check CHECK (kind_probs IS NULL OR jsonb_typeof(kind_probs) = 'object');
