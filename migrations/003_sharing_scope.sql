-- Migration 003: Per-operator sharing scope
-- Replaces the global sharing boolean with per-operator scoping.
-- Modes: 'none' (no sharing), 'intra' (same operator only), 'full' (cross-operator)

ALTER TABLE operators
  ADD COLUMN sharing_scope VARCHAR(5) NOT NULL DEFAULT 'none'
  CHECK (sharing_scope IN ('none', 'intra', 'full'));
