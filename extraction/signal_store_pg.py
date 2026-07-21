"""Postgres writer for ExtractionBatches, and the pooling policy.

write_batch(dsn, batch, fallback_users) inserts, idempotently:
  extraction_batches -> signals -> evidence_refs -> signal_pool_events

Idempotency: batch_id and signal_id are content hashes, so a re-run inserts
with ON CONFLICT DO NOTHING and pool events are only written for signals that
were newly inserted -- replaying a window never double-pools.

Pooling policy: a signal is pooled to every distinct owner_candidate
person_ref (their boards may care). If a signal has no resolvable owners, it
pools to fallback_users -- the window's participants -- because unattributed
work spoken in a conversation still belongs on someone in that conversation's
review card, and the reconciler/reviewer sorts out whose.

Python 3.8 compatible. Requires psycopg2.
"""

import json

import psycopg2


def write_batch(dsn, batch, fallback_users):
    """Returns {"signals_inserted": n, "pool_events": m, "already_present": k}."""
    conn = psycopg2.connect(dsn)
    out = {"signals_inserted": 0, "pool_events": 0, "already_present": 0}
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
                           blocker_party, owner_candidates, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (signal_id) DO NOTHING""",
                        (s["signal_id"], batch["batch_id"], s["type"],
                         s["title"], s.get("description"),
                         s.get("status_signal"), s["confidence"],
                         d.get("date"), d.get("precision"), d.get("raw_text"),
                         b.get("description"), b.get("blocking_party_ref"),
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
                    owners = sorted({c["person_ref"]
                                     for c in s.get("owner_candidates") or []})
                    targets = owners or sorted(set(fallback_users))
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
