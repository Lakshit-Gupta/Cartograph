"""sentence-transformers MiniLM (~250MB resident, ARM64 friendly)."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from functools import lru_cache

import numpy as np

from src.common.logger import get_logger

_log = get_logger(__name__)

MODEL_NAME = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
CACHE_DIR = os.environ.get("EMBED_CACHE", "/app/.cache/models")


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer
    _log.info("loading_embedding_model", name=MODEL_NAME, cache=CACHE_DIR)
    return SentenceTransformer(MODEL_NAME, cache_folder=CACHE_DIR)


async def embed_one(text: str) -> list[float]:
    return (await embed_batch([text]))[0]


async def embed_batch(texts: Iterable[str]) -> list[list[float]]:
    items = [t or "" for t in texts]

    def _run() -> list[list[float]]:
        m = _model()
        vecs = m.encode(items, batch_size=32, normalize_embeddings=True, convert_to_numpy=True)
        return vecs.tolist()

    return await asyncio.to_thread(_run)


def cosine(a: list[float], b: list[float]) -> float:
    av = np.asarray(a, dtype=np.float32)
    bv = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(av))
    nb = float(np.linalg.norm(bv))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(av, bv) / (na * nb))
