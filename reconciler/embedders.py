"""Embedders for alias-set matching.

The reconciler needs one callable: embed(list_of_texts) -> list_of_vectors.
Two implementations:

  OllamaEmbedder   real embeddings via a local Ollama model
                   (nomic-embed-text is a good default on a Mac)
  TokenEmbedder    deterministic, dependency-free bag-of-tokens vectors.
                   Cosine on these tracks lexical overlap, which is exactly
                   what tests need to place similarities in specific bands
                   on purpose. Also a serviceable zero-cost fallback.

Cosine similarity helper included; vectors are plain Python lists so no
numpy dependency. Python 3.8 compatible.
"""

import hashlib
import json
import math
import re
import urllib.request

_WORD = re.compile(r"[a-z0-9]+")

_STOP = {
    "the", "a", "an", "to", "for", "of", "in", "on", "and", "or", "is",
    "are", "be", "with", "by", "at", "it", "this", "that", "up", "out",
}


def cosine(u, v):
    dot = sum(a * b for a, b in zip(u, v))
    nu = math.sqrt(sum(a * a for a in u))
    nv = math.sqrt(sum(b * b for b in v))
    if nu == 0 or nv == 0:
        return 0.0
    return dot / (nu * nv)


class TokenEmbedder(object):
    """Hashed bag-of-tokens vectors. Deterministic, offline, fast."""

    def __init__(self, dim=512):
        self.dim = dim

    def __call__(self, texts):
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _WORD.findall((text or "").lower()):
                if tok in _STOP:
                    continue
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            out.append(vec)
        return out


class OllamaEmbedder(object):
    """Embeddings via Ollama's /api/embeddings, one call per text."""

    def __init__(self, model="nomic-embed-text",
                 host="http://localhost:11434"):
        self.model = model
        self.host = host

    def __call__(self, texts):
        out = []
        for text in texts:
            body = json.dumps({"model": self.model,
                               "prompt": text or ""}).encode("utf-8")
            req = urllib.request.Request(
                self.host + "/api/embeddings", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out.append(data["embedding"])
        return out
