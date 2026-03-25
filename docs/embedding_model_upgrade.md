# Embedding Model Upgrade: gte-small to EmbeddingGemma-300M

**Purpose:** Claude Code task. Swap embedding model, resize pgvector column, re-embed all atoms, recalibrate thresholds.

**Servers affected:** mnemo-net (prod, ~247 atoms)

---

## Why

gte-small similarity range compresses at scale. At 5,839 atoms the effective range collapsed to 0.031 (floor 0.818, ceiling 0.854). Unrelated queries score above 0.82. EmbeddingGemma-300M has 4.3x wider effective range (0.135) with a floor of 0.254.

---

## Pre-Flight

### 1. Backup (CRITICAL)

On mnemo-net:
```bash
sudo /root/.local/bin/borgmatic create --verbosity 1 --list
sudo /root/.local/bin/borgmatic list
```


### 2. Confirm model loads

```bash
uv run python -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('google/embeddinggemma-300m')
emb = model.encode('test sentence')
print(f'Dimensions: {len(emb)}')
print(f'Model loaded OK')
"
```

If dependencies are missing:
```bash
pip install --upgrade "transformers>=4.51.0" "sentence-transformers>=2.7.0" --break-system-packages
```

---

## Step 1: Schema Migration

The embedding column changes from vector(384) to vector(768).

```sql
sudo -u postgres psql -d mnemo
```

Check current state:
```sql
\d atoms

SELECT column_name, is_nullable
FROM information_schema.columns
WHERE table_name = 'atoms' AND column_name = 'embedding';

-- Check for indexes on embedding
SELECT indexname, indexdef FROM pg_indexes
WHERE tablename = 'atoms' AND indexdef LIKE '%embedding%';
```

Migrate:
```sql
-- Drop index if present (note actual name from query above)
DROP INDEX IF EXISTS idx_atoms_embedding;

-- If NOT NULL constraint exists:
ALTER TABLE atoms ALTER COLUMN embedding DROP NOT NULL;

-- Resize
ALTER TABLE atoms ALTER COLUMN embedding TYPE vector(768);

-- Verify: should show embedding vector(768)
\d atoms
```

Re-add NOT NULL and recreate index after re-embedding (Step 3).

---

## Step 2: Update Server Config

Find current model reference:
```bash
grep -r "gte-small\|thenlper" --include="*.py" /home/mnemo/mnemo-server/
```

Change to EmbeddingGemma. Make configurable via env:

```python
EMBEDDING_MODEL = os.getenv("MNEMO_EMBEDDING_MODEL", "google/embeddinggemma-300m")
EMBEDDING_DIMS = int(os.getenv("MNEMO_EMBEDDING_DIMS", "768"))
```

Add to .env:
```
MNEMO_EMBEDDING_MODEL=google/embeddinggemma-300m
MNEMO_EMBEDDING_DIMS=768
```

**IMPORTANT: EmbeddingGemma uses task-specific prompts.** This is a key difference from gte-small which used no prompts. The embedding code must be updated:

- Storing atoms: model.encode(text, prompt_name="document")
- Recall queries: model.encode(text, prompt_name="query")

Verify the prompts are configured:
```python
model = SentenceTransformer("google/embeddinggemma-300m")
print(model.prompts)
# Should show query and document prompt templates
```

If the current code just calls model.encode(text) with no prompt_name, BOTH the store path AND the recall path need updating. This is the most important code change in the entire migration.

---

## Step 3: Re-embed All Atoms

Update the existing scripts/re_embed.py to use EmbeddingGemma with prompt_name="document".

```bash
# Dry run
uv run scripts/re_embed.py

# Apply
uv run scripts/re_embed.py --apply
```

Timing:
- mnemo-net (247 atoms at 37ms): ~9 seconds

After re-embedding:
```sql
ALTER TABLE atoms ALTER COLUMN embedding SET NOT NULL;

-- Recreate index if needed (for <10K atoms exact search is fine, skip this):
-- CREATE INDEX idx_atoms_embedding ON atoms USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

---

## Step 4: Recalibrate Thresholds

Find where each is set:
```bash
grep -rn "min_similarity\|MIN_SIMILARITY\|dedup\|DEDUP\|similarity_drop\|0\.7.*0\.3\|edge.*threshold" --include="*.py" /home/mnemo/mnemo-server/
```

### min_similarity
- Current: 0.75 (gte-small tuned)
- New: **0.25**
- Rationale: EmbeddingGemma floor for unrelated content is ~0.19-0.25
- Env: MNEMO_MIN_SIMILARITY=0.25

### Dedup threshold
- Current: 0.97 (forced high for gte-small)
- New: **0.90**
- Rationale: EmbeddingGemma showed near-zero false collisions at 0.90
- Env: MNEMO_DEDUP_THRESHOLD=0.90

### Composite scoring weights
- Current: similarity * (0.7 + 0.3 * c_eff)
- New: **Leave at 0.7/0.3 for now**
- Make configurable, tune empirically after LoCoMo re-run
- Env: MNEMO_SIMILARITY_WEIGHT=0.7  MNEMO_CONFIDENCE_WEIGHT=0.3

### Edge creation threshold (if applicable)
- Current: 0.85
- New: **0.55**
- Rationale: scale proportionally with effective range shift

### similarity_drop_threshold (recall gap detection)
- Current: 0.3
- New: **0.15**
- Rationale: 0.3 drop in EmbeddingGemma range is a much larger relative gap

---

## Step 5: Restart

```bash
cd /home/mnemo/mnemo-server
docker compose down
docker compose build
docker compose up -d

curl -s https://api.mnemo-ai.com/health | python3 -m json.tool
```

---

## Step 6: Validate

### Smoke test
- Store a new memory via MCP or API
- Recall it: should return with similarity ~0.35+
- Recall unrelated query: should return low scores or empty
- Verify scores are in expected range: 0.30-0.45 for good matches, 0.15-0.25 for noise

### Dogfood 48 hours
- Are recall results more relevant?
- Is noise reduced?
- Are atoms deduping correctly at 0.90?

---

## Rollback

1. Restore from borgmatic backup (mnemo-net) 
2. Revert model config to thenlper/gte-small and dims to 384
3. Revert embedding column to vector(384)
4. Revert threshold configs
5. Restart

Entire rollback: ~5 minutes.

---

## Sequence Summary

```
1.  Backup                             2 min
2.  Test model loads                   1 min
3.  Schema: resize vector column       1 min
4.  Update server config + .env        5 min
5.  Update embedding code (prompts!)  10 min
6.  Re-embed all atoms                10 sec on mnemo-net
7.  Recalibrate thresholds             5 min
8.  Rebuild + restart Docker           2 min
9.  Smoke test                         5 min
```

Total: ~1 hour focused work on mnemo-net

---

## Post-Upgrade Checklist

- [ ] pyproject.toml: add transformers>=4.51.0 if needed
- [ ] Docker image: pre-download EmbeddingGemma so cold starts skip HuggingFace
- [ ] Update docs referencing gte-small or 384 dimensions
- [ ] Health endpoint: update embedding_model and embedding_dimensions
- [ ] Commit and push all changes
- [ ] Run composite weight experiments (0.6/0.4, 0.5/0.5) after LoCoMo baseline
