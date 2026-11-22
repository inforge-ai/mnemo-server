import asyncio
import hashlib
import logging
import os
import re
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np

from .config import settings

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2)


@lru_cache(maxsize=1)
def _get_model():
    from sentence_transformers import SentenceTransformer
    logger.info("Loading embedding model %s (once at startup)", settings.embedding_model)
    try:
        model = SentenceTransformer(settings.embedding_model, local_files_only=True)
    except OSError:
        logger.info("Model not cached locally, downloading %s", settings.embedding_model)
        model = SentenceTransformer(settings.embedding_model)
    logger.info("Embedding model ready")
    return model


def warmup() -> None:
    """Pre-load the model at server startup so the first request isn't slow."""
    if settings.embedding_backend == "fake":
        return
    _get_model()


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fake_encode(text: str) -> list[float]:
    """Deterministic hashed bag-of-words embedding (tests only).

    Lowercased word tokens and adjacent-word bigrams are hashed into
    `embedding_dim` buckets and L2-normalised. Identical text gives identical
    vectors and lexical overlap gives graded cosine similarity, so dedup,
    recall and edge tests stay meaningful without the gated model.
    """
    dim = settings.embedding_dim
    vec = np.zeros(dim, dtype=np.float64)
    tokens = _TOKEN_RE.findall(text.lower())
    features = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    for feat in features:
        h = hashlib.blake2b(feat.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    if norm == 0:
        # Empty / punctuation-only text: a fixed unit vector.
        vec[0] = 1.0
        norm = 1.0
    return (vec / norm).tolist()


def _encode_sync(text: str, prompt_name: str = "document") -> list[float]:
    if settings.embedding_backend == "fake":
        return _fake_encode(text)
    model = _get_model()
    kwargs: dict = {"normalize_embeddings": True, "show_progress_bar": False}
    if model.prompts:
        kwargs["prompt_name"] = prompt_name
    vector = model.encode(text, **kwargs)
    return vector.tolist()


async def encode(text: str, prompt_name: str = "document") -> list[float]:
    # In test mode, run encoding synchronously to avoid yielding the event
    # loop to pytest-asyncio fixture setup (clean_db) mid-request.
    if settings.sync_store_for_tests:
        return _encode_sync(text, prompt_name)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _encode_sync, text, prompt_name)


def _encode_batch_sync(texts: list[str], prompt_name: str = "document") -> list[list[float]]:
    if settings.embedding_backend == "fake":
        return [_fake_encode(t) for t in texts]
    model = _get_model()
    kwargs: dict = {"normalize_embeddings": True, "show_progress_bar": False}
    if model.prompts:
        kwargs["prompt_name"] = prompt_name
    vectors = model.encode(list(texts), **kwargs)
    return [v.tolist() for v in vectors]


async def encode_batch(texts: list[str], prompt_name: str = "document") -> list[list[float]]:
    """Embed several texts in one model call. Order is preserved."""
    if not texts:
        return []
    if settings.sync_store_for_tests:
        return _encode_batch_sync(texts, prompt_name)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _encode_batch_sync, texts, prompt_name)
