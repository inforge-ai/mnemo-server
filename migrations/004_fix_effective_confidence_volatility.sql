-- Migration 004: Fix effective_confidence volatility
-- The function calls now() internally, which violates the IMMUTABLE contract.
-- IMMUTABLE allows PostgreSQL to cache/constant-fold results, causing stale
-- decay calculations. STABLE is correct: consistent within a statement.

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
$$ LANGUAGE plpgsql STABLE;

-- Widen version column for longer migration names
ALTER TABLE schema_migrations ALTER COLUMN version TYPE VARCHAR(128);

-- Track migration
INSERT INTO schema_migrations (version) VALUES ('004_fix_effective_confidence_volatility') ON CONFLICT DO NOTHING;
