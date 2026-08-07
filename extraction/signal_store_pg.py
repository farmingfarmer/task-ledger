"""Postgres writer for ExtractionBatches, and the pooling policy.

write_batch(dsn, batch, fallback_users) inserts, idempotently:
  extraction_batches -> signals -> evidence_refs -> signal_pool_events

Idempotency: batch_id and signal_id are content hashes, so a re-run inserts
with ON CONFLICT DO NOTHING and pool events are only written for signals that
were newly inserted -- replaying a window never double-pools.

Pooling policy: a signal is pooled to every distinct owner_candidate
person_ref (their boards may care). What happens when there are none is the
part that had to change.

It used to fall back to the window's participants -- everyone in the
conversation -- on the theory that somebody there would sort out whose it
was. Measured against the first live run, that theory cost: one sentence
("can we prioritise the readme") became a create proposal on ten boards,
and nine people correctly rejected it as not theirs. 15 of 22 wrong_owner
rejections were that shape, and matching_policy.yaml had said not to do it
all along -- "route to team-level proposals pool, NOT any individual's
packet".

So an unowned signal now pools to its AUTHOR: the one person known to be
connected to the claim, because they said it. They can accept it (naming
an owner as they go), reject it once, or leave it. Ten review cards become
one. A signal with neither owner nor author is not routed at all: it stays
in the ledger, is reported in the return value, and lands on nobody's card
-- which is the honest outcome for a claim the system cannot attribute to
anyone, and is vanishingly rare now that author_ref is recorded.

broadcast_unowned=True restores the old fan-out for anyone who wants it.

Python 3.8 compatible. Requires psycopg2.
"""

import json

import psycopg2


def pool_targets(signal, fallback_users, broadcast_unowned=False):
    """Who should see this claim. Pure, so the routing rule is testable
    without a database -- it is the rule that decides how many people get
    asked about one sentence."""
    owners = sorted({(c or {}).get("person_ref")
                     for c in signal.get("owner_candidates") or []
                     if (c or {}).get("person_ref")})
    if owners:
        return owners, "owners"
    author = signal.get("author_ref")
    if author:
        return [author], "author"
    if broadcast_unowned:
        return sorted(set(fallback_users or [])), "broadcast"
    return [], "unrouted"


def write_batch(dsn, batch, fallback_users, broadcast_unowned=False):
    """Returns {"signals_inserted": n, "pool_events": m, "already_present": k,
    "routed_to_author": n, "unrouted": n}."""
    conn = psycopg2.connect(dsn)
    out = {"signals_inserted": 0, "pool_events": 0, "already_present": 0,
           "routed_to_author": 0, "unrouted": 0}
    try:
        with conn:
            with conn.cursor() as cur:
                w = batch["window"]
                cur.execute("""
                    INSERT INTO extraction_batches
                      (batch_id, extracted_at, extractor_model, prompt_version,
                       source_type, source_ref, window_start, window_end)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (batch_id) DO NOTHING""",
                    (batch["batch_id"], batch["extracted_at"],
                     batch["extractor"]["model"],
                     batch["extractor"]["prompt_version"],
                     w["source_type"], w["source_ref"],
                     w["time_start"], w["time_end"]))
                for s in batch["signals"]:
                    d = s.get("deadline") or {}
                    b = s.get("blocker") or {}
                    cur.execute("""
                        INSERT INTO signals
                          (signal_id, batch_id, signal_type, title, description,
                           status_signal, confidence, deadline_date,
                           deadline_precision, deadline_raw, blocker_desc,
                           blocker_party, author_ref, owner_candidates, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (signal_id) DO NOTHING""",
                        (s["signal_id"], batch["batch_id"], s["type"],
                         s["title"], s.get("description"),
                         s.get("status_signal"), s["confidence"],
                         d.get("date"), d.get("precision"), d.get("raw_text"),
                         b.get("description"), b.get("blocking_party_ref"),
                         s.get("author_ref"),
                         json.dumps(s.get("owner_candidates") or []),
                         json.dumps(s)))
                    if cur.rowcount == 0:
                        out["already_present"] += 1
                        continue
                    out["signals_inserted"] += 1
                    for i, ev in enumerate(s["evidence"], start=1):
                        span = ev.get("span") or {}
                        cur.execute("""
                            INSERT INTO evidence_refs
                              (signal_id, ordinal, source_type, resource_id,
                               span_start, span_end, content_hash)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT DO NOTHING""",
                            (s["signal_id"], i, ev["source_type"],
                             ev["resource_id"], span.get("start"),
                             span.get("end"), ev.get("content_hash")))
                    targets, how = pool_targets(s, fallback_users,
                                                broadcast_unowned)
                    if how == "author":
                        out["routed_to_author"] += 1
                    elif how == "unrouted":
                        out["unrouted"] += 1
                    for user in targets:
                        cur.execute("""
                            INSERT INTO signal_pool_events
                              (signal_id, user_ref, event, run_id)
                            VALUES (%s,%s,'pooled',%s)""",
                            (s["signal_id"], user, batch["batch_id"]))
                        out["pool_events"] += 1
        return out
    finally:
        conn.close()
