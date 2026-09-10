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
                 host="http://127.0.0.1:11434"):
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

class CachedEmbedder(object):
    """Wraps any embedder with the embedding_cache table.

    An embedding is a pure function of (model, text), and this system's
    texts barely change between ticks -- task titles, aliases, standing
    candidates. Before this wrapper, every tick recomputed every one of
    them for every user, on a CPU, and threw the vectors away. Now a text
    is embedded once per model, ever.

    Deliberately boring about failure: a missing table, a dead
    connection, an unwritable row all degrade to "just call the inner
    embedder" -- a cache must never be the reason a reconcile fails. The
    one thing it will not do is serve a vector across models: the key
    includes the model name, so swapping embedders misses cleanly instead
    of poisoning silently.
    """

    def __init__(self, inner, dsn, model_name):
        self.inner = inner
        self.dsn = dsn
        self.model = model_name
        self._off = False
        self.calls_saved = 0

    def _sha(self, text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def __call__(self, texts):
        if self._off or not texts:
            return self.inner(texts)
        try:
            import psycopg2
            conn = psycopg2.connect(self.dsn, connect_timeout=5)
        except Exception:                               # noqa: BLE001
            self._off = True
            return self.inner(texts)
        try:
            shas = [self._sha(t) for t in texts]
            found = {}
            with conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT text_sha, vec FROM embedding_cache "
                    "WHERE model = %s AND text_sha = ANY(%s)",
                    (self.model, list(set(shas))))
                for sha, vec in cur.fetchall():
                    found[sha] = vec
            miss_idx = [i for i, sha in enumerate(shas)
                        if sha not in found]
            if miss_idx:
                fresh = self.inner([texts[i] for i in miss_idx])
                with conn, conn.cursor() as cur:
                    for i, vec in zip(miss_idx, fresh):
                        found[shas[i]] = vec
                        cur.execute(
                            "INSERT INTO embedding_cache "
                            "(model, text_sha, vec) VALUES (%s,%s,%s) "
                            "ON CONFLICT DO NOTHING",
                            (self.model, shas[i], json.dumps(vec)))
            self.calls_saved += len(texts) - len(miss_idx)
            return [found[sha] for sha in shas]
        except Exception:                               # noqa: BLE001
            self._off = True
            return self.inner(texts)
        finally:
            conn.close()

