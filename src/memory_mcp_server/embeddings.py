"""Ollama embedding calls with LRU cache and graceful degradation."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

import urllib.request
import urllib.error
import structlog

from memory_mcp_server.config import settings

log = structlog.get_logger(__name__)

EXPECTED_DIM = 768


@lru_cache(maxsize=128)
def _embed_cached(text: str) -> Optional[tuple]:
    """Cached embedding — returns tuple (hashable) or None.

    Supports both Ollama API styles:
      - New (>=0.1.26): POST /api/embed  { model, input }  → { embeddings: [[...]] }
      - Legacy:         POST /api/embeddings { model, prompt } → { embedding: [...] }
    """
    url = f"{settings.ollama_base_url}/api/embed"
    payload = json.dumps({
        "model": settings.ollama_embed_model,
        "input": text,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            # New API: embeddings is a list of lists
            embeddings = data.get("embeddings")
            if embeddings and isinstance(embeddings, list):
                vec = embeddings[0]
            else:
                # Legacy fallback
                vec = data.get("embedding", [])
            if len(vec) != EXPECTED_DIM:
                log.warning("Unexpected embedding dim", got=len(vec), expected=EXPECTED_DIM)
                return None
            return tuple(vec)
    except urllib.error.URLError as e:
        log.warning("Ollama unreachable", error=str(e))
        return None
    except Exception as e:
        log.warning("Embedding error", error=str(e))
        return None


def embed(text: str) -> Optional[list[float]]:
    """Return 768-dim embedding list, or None on failure."""
    result = _embed_cached(text.strip())
    return list(result) if result is not None else None


def check_ollama() -> bool:
    """Return True if Ollama is reachable."""
    vec = embed("health check")
    return vec is not None
