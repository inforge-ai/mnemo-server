import asyncio
import logging
import os
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

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
    _get_model()


def _encode_sync(text: str, prompt_name: str = "document") -> list[float]:
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
