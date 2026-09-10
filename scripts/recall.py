#!/usr/bin/env python3
"""Answer "who said that, and when?" from the spoken record, with receipts.

    DB_DSN=... python3 scripts/recall.py "synthetic data"
    DB_DSN=... python3 scripts/recall.py --embed-model nomic-embed-text "who took the S3 bucket work"

Read-only. `ask` answers from CONFIRMED board state -- what the team has
agreed is true. This answers from what was SAID, which is a different and
older kind of truth: the disputed decision, the promise somebody
half-remembers, the "I'm sure we discussed this". Every hit is a quote
with a speaker, a meeting, a date and its evidence hash, plus what became
of it -- tracked, finished, waiting, or never picked up. Settling a
who-said-what dispute in one command, with a citation, is the whole point.

Matching is lexical by default (works with everything off) and semantic
when an embed model is named and Ollama answers. Neither writes a word of
the answer: rows are quoted, never paraphrased.
"""
import argparse
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for _s in ("reconciler", "scripts"):
    _p = os.path.join(ROOT, _s)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg2                                         # noqa: E402
import psycopg2.extras                                  # noqa: E402
import serieslib                                        # noqa: E402


def fate(cur, signal_id):
    """What became of a sentence, in one clause."""
    cur.execute("""
        SELECT t.title, t.status
        FROM proposal_signals ps
        JOIN decisions d ON d.proposal_id = ps.proposal_id
                        AND d.decision IN ('accept')
        JOIN task_events e ON e.decision_id = d.decision_id
        JOIN tasks_current t ON t.task_id = e.task_id
        WHERE ps.signal_id = %s LIMIT 1""", (signal_id,))
    r = cur.fetchone()
    if r:
        return "became “{0}” ({1})".format(r["title"], r["status"])
    cur.execute("""
        SELECT t.title FROM task_aliases a
        JOIN tasks_current t USING (task_id)
        WHERE a.source_signal_id = %s LIMIT 1""", (signal_id,))
    r = cur.fetchone()
    if r:
        return "a known wording of “{0}”".format(r["title"])
    cur.execute("""
        SELECT c.state, c.sightings FROM task_candidate_events e
        JOIN v_task_candidates c USING (candidate_id, user_ref)
        WHERE e.signal_id = %s LIMIT 1""", (signal_id,))
    r = cur.fetchone()
    if r:
        return {"sighted": "registered, {0} sighting(s), not yet tracked"
                .format(r["sightings"]),
                "promoted": "sent to a review queue",
                "accepted": "tracked",
                "dismissed": "ruled not a task"}.get(r["state"], r["state"])
    return "never picked up"


def main():
    ap = argparse.ArgumentParser(
        description="Search what was said, answer with receipts. Read-only.")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--embed-model", default=os.environ.get("TL_EMBED_MODEL"))
    ap.add_argument("--ollama-host", default=os.environ.get(
        "OLLAMA_HOST_URL", "http://127.0.0.1:11434"))
    args = ap.parse_args()
    query = " ".join(args.query)
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset.")
        return 2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        print("database unreachable ({0}) -- start it (deploy/windows/"
              "pg_start.bat on Windows) and re-run."
              .format(exc.__class__.__name__))
        return 2
    try:
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.signal_id, s.signal_type, s.title,
                       s.description, s.author_ref,
                       b.source_ref, b.window_start,
                       (SELECT e.resource_id FROM evidence_refs e
                         WHERE e.signal_id = s.signal_id
                         ORDER BY e.ordinal LIMIT 1) AS resource_id,
                       (SELECT e.content_hash FROM evidence_refs e
                         WHERE e.signal_id = s.signal_id
                         ORDER BY e.ordinal LIMIT 1) AS content_hash
                FROM signals s
                JOIN extraction_batches b USING (batch_id)
                ORDER BY b.window_start DESC LIMIT 3000""")
            rows = cur.fetchall()

            scored, mode = [], "lexical"
            if args.embed_model:
                try:
                    import embedders
                    emb = embedders.OllamaEmbedder(args.embed_model,
                                                   args.ollama_host)
                    texts = [(r["title"] or "") + " "
                             + (r["description"] or "") for r in rows]
                    vecs = emb([query] + texts)
                    qv, rv = vecs[0], vecs[1:]
                    scored = sorted(
                        ((embedders.cosine(qv, v), r)
                         for v, r in zip(rv, rows)),
                        key=lambda p: -p[0])
                    mode = "semantic ({0})".format(args.embed_model)
                except Exception as exc:                # noqa: BLE001
                    print("(embedder unavailable, {0!r} -- falling back to "
                          "words)".format(exc))
            if not scored:
                words = [w for w in query.lower().split() if len(w) > 2]
                def hits(r):
                    text = ((r["title"] or "") + " "
                            + (r["description"] or "")).lower()
                    return sum(1 for w in words if w in text)
                scored = sorted(((hits(r), r) for r in rows),
                                key=lambda p: -p[0])
                scored = [(s, r) for s, r in scored if s > 0]

            top = scored[:args.limit]
            if not top:
                print("Nothing in the spoken record matches "
                      "“{0}”.".format(query))
                return 0
            print("“{0}” · {1} · {2} of {3} sentence(s) "
                  "searched\n".format(query, mode, len(top), len(rows)))
            for score, r in top:
                who = (r["author_ref"] or "speaker not recorded").replace(
                    "aad-", "")
                when = str(r["window_start"])[:10] if r["window_start"] \
                    else "date unknown"
                print("  “{0}”".format(r["title"]))
                if r["description"]:
                    print("      {0}".format(r["description"][:100]))
                print("      -- {0}, in {1}, {2}".format(
                    who, serieslib.pretty(r["source_ref"]), when))
                print("      receipt: {0} {1} · {2}".format(
                    r["resource_id"] or "?",
                    (r["content_hash"] or "")[:12],
                    fate(cur, r["signal_id"])))
                print("")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
