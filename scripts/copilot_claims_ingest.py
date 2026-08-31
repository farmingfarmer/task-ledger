#!/usr/bin/env python3
"""Ingest Copilot's own structured task claims as signals.

Copilot will export per-person claims already structured:

    {"person": "Robin Vale", "project": "Intake Platform",
     "task": "Run broader testing suite", "status": "in_progress",
     "progress": "Prototype tested against exports",
     "blockers": ["Needs a larger dataset"],
     "next_action": "Continue fidelity testing",
     "claim_type": "progress_update",
     "evidence_strength": "transcript_supported",
     "confidence": "high",
     "sources": ["Weekly Sprint"]}

This is a better input than a recap and, for extraction quality, better
than raw transcript in places: it is specific, it names a person per claim,
and it separates progress from blockers from next actions.

    python3 scripts/copilot_claims_ingest.py claims.json --dry-run
    python3 scripts/copilot_claims_ingest.py claims.json --dsn "$TL_COPILOT_DSN"

The prompt that produces this shape, and the vocabularies it must hit,
are in docs/prompts/copilot-claims-prompt.md. The values below are
PARSED: a claim_type outside the map degrades to task_mention, which
proposes nothing, so the prompt's wording is load-bearing.

THIS DOES NOT RUN THE EXTRACTOR. Copilot already extracted; re-extracting
its output would be a second lossy pass over a paraphrase. This writes the
same batch shape the worker writes, validated against the same contract, so
everything downstream -- pooling, reconciliation, gates, review, the board
-- is unchanged. Copilot becomes the extractor; the ledger stays the system
of record.

THE THREE HONESTY RULES, which are the whole reason this is a separate
source type rather than a shortcut into the existing one:

1. "evidence_strength": "transcript_supported" is an assertion that
   evidence exists, not evidence. There is no message id, no utterance, no
   span; sources are MEETING NAMES, sometimes two for one claim. The
   assistant that wrote it will not release the transcript, so nobody
   downstream can check it. These land as evidence type 'assistant_claim',
   which the provenance rail must never offer as a trace-to-source, and the
   hash is over the exported record -- which makes a quietly edited export
   detectable, the one thing a hash can honestly do here.

2. Confidence is CAPPED, at a level chosen against the op floors rather
   than by feel. Copilot's "high" is its own uncalibrated word. The cap
   sits above the floors for create/status/blocker -- those must reach a
   human, that is the point -- and below the 0.80 and 0.85 floors for
   complete, cancel and merge, so an unverifiable claim is structurally
   incapable of proposing that work is finished or thrown away.

3. Owner basis is always 'inferred'. explicit_assignment and
   self_reference mean somebody was heard saying something, and nothing
   here was heard by this system. Nothing is lost: the assistant's own
   claim_type, evidence_strength and self-rated confidence are written into
   the signal's description, where the person deciding actually reads them
   -- burying them in a raw blob would hide the unverifiability from the
   one person who needs it. They are simply not promoted to a certainty
   they have not got.

Deterministic ids: re-ingesting the same export is an upsert-shaped no-op.
Python 3.8, stdlib only (jsonschema for validation, as everywhere else).
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "extraction"))

# Copilot's vocabulary -> ours. Anything unrecognised becomes a sighting
# rather than being forced into a stronger type.
CLAIM_TYPE = {
    "explicit_commitment": "assignment",
    "commitment": "assignment",
    "progress_update": "status_update",
    "status_change": "status_update",
    "status_update": "status_update",
    "blocker": "blocker",
    "blocker_resolved": "blocker_resolved",
    "completion": "completion_claim",
    "completion_claim": "completion_claim",
    "coordination": "task_mention",
    "mention": "task_mention",
}

STATUS = {
    "in_progress": "in_progress", "open": "proposed", "backlog": "proposed",
    "waiting": "blocked", "blocked": "blocked", "done": "completed",
    "completed": "completed", "cancelled": "cancelled",
}

# Calibrated against policy/matching_policy.yaml's per_op_confidence_floor,
# and the first attempt at this was wrong in an instructive way. Capping at
# 0.60 -- the floor for create_task, update_status and add_blocker -- meant
# these claims never reached a review card at all, which confuses "must not
# outrank a verifiable claim" with "must not reach a person". Reaching a
# person is the entire point.
#
# The cap now sits above those floors and below the ones that matter:
# complete_task and cancel_task require 0.80, merge_tasks 0.85. So an
# unverifiable claim can propose that work exists, moved, or is blocked --
# all reversible, all reviewed -- and is structurally incapable of
# proposing that work is finished, cancelled or merged away. The highest
# blast radius ops stay reachable only from evidence somebody can open.
CONFIDENCE = {"high": 0.75, "medium": 0.62, "low": 0.45}
MAX_CONFIDENCE = 0.75

PROMPT_VERSION = "copilot-claims-v1"


def sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def ref_for(display):
    return "aad-" + re.sub(r"[^a-z0-9]+", "-",
                           (display or "unknown").lower()).strip("-")


def slug(text, n=40):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text or "").strip("_")[:n] or "x"


def claim_signal(rec, batch_id, idx, bootstrap=False):
    """One exported claim -> one signal, or (None, reason).

    bootstrap matters more than it looks. A Copilot export is mostly
    progress and blockers on work already under way, and against an EMPTY
    ledger those have nothing to attach to: update_status and add_blocker
    need a target task, so they are consumed and propose nothing, and a
    first import of twenty claims yields one proposal. Bootstrap maps every
    claim to a type that can create -- a commitment stays an assignment,
    everything else becomes a proposal -- so the first import can establish
    that the work exists. The claim's real character is not lost: the
    status still rides along, and the description still says what Copilot
    called it.
    """
    if not isinstance(rec, dict):
        # Belt and braces: whatever the scanner lets through, one bad
        # entry must cost that entry and not the import.
        return None, "not a claim object"
    person = (rec.get("person") or "").strip()
    task = (rec.get("task") or "").strip()
    if not task:
        return None, "no task text"
    if not person:
        # Everything here is per-person by construction; a record without
        # one is malformed, and guessing an owner is the failure this
        # system exists to avoid.
        return None, "no person named"

    raw_type = (rec.get("claim_type") or "").strip().lower()
    stype = CLAIM_TYPE.get(raw_type, "task_mention")
    blockers = [b for b in (rec.get("blockers") or []) if b]
    if bootstrap:
        # Only these two create a task. task_mention explicitly proposes
        # nothing by itself (matching_policy.yaml), so leaving a bootstrap
        # claim as one would drop it on the floor.
        stype = ("assignment" if stype == "assignment" else "proposal")
    # A claim typed as anything else but carrying blockers still records
    # them; the type says what the claim was ABOUT, the blocker field says
    # what is in the way.
    if not bootstrap and stype != "blocker" and blockers \
            and not rec.get("progress"):
        stype = "blocker"

    conf = CONFIDENCE.get((rec.get("confidence") or "").strip().lower(), 0.40)
    conf = min(conf, MAX_CONFIDENCE)

    sources = [s for s in (rec.get("sources") or []) if s]
    # The pointer names what Copilot named -- a meeting, not a message --
    # and says so in its own id, so nothing downstream can mistake it for a
    # retrievable utterance.
    resource_id = "copilot-claim:" + sha(person, task,
                                         "|".join(sources))[:16]
    signal_id = sha(batch_id, str(idx), person, task, stype)[:32]

    # The assistant's own words about what this claim was and how sure it
    # said it was. The contract forbids extra keys on a signal, and burying
    # them in a raw blob would hide them from the person deciding -- which
    # is exactly who needs to see that the citation is unverifiable.
    asserted = "Copilot: {0}, evidence {1} (UNVERIFIED - no retrievable " \
               "source), self-rated {2}{3}".format(
                   rec.get("claim_type") or "unspecified",
                   rec.get("evidence_strength") or "unstated",
                   rec.get("confidence") or "unstated",
                   (", sources: " + "; ".join(sources)) if sources else "")
    description = " ".join(x for x in [
        rec.get("progress"),
        ("Blocked on: " + "; ".join(blockers)) if blockers else None,
        ("Next: " + rec["next_action"]) if rec.get("next_action") else None,
        asserted,
    ] if x)[:600] or None

    signal = {
        "signal_id": signal_id,
        "type": stype,
        "title": task[:120],
        "description": description,
        "status_signal": STATUS.get((rec.get("status") or "").strip().lower()),
        "confidence": conf,
        "author_ref": None,          # nobody was heard; there is no speaker
        "owner_candidates": [{
            "person_ref": ref_for(person),
            # Rule 3: never promoted, whatever Copilot called it.
            "basis": "inferred",
            "confidence": conf,
        }],
        "deadline": None,
        "blocker": ({"description": "; ".join(blockers)[:300],
                     "blocking_party_ref": None} if blockers else None),
        "evidence": [{
            "source_type": "assistant_claim",
            "resource_id": resource_id,
            "span": None,
            # Hash of the record as exported: this cannot prove what anyone
            # said, but it does prove the export was not edited afterwards.
            "content_hash": sha(json.dumps(rec, sort_keys=True)),
        }],
    }
    return signal, None


def build_batch(records, exported_at, label, bootstrap=False):
    source_ref = "copilot-claims-{0}-{1}".format(exported_at[:10], slug(label,
                                                                       24))
    batch_id = sha(source_ref, exported_at, PROMPT_VERSION)[:32]
    batch = {
        "batch_id": batch_id,
        "extracted_at": exported_at,
        "extractor": {"model": "copilot:claims-export",
                      "prompt_version": PROMPT_VERSION},
        "window": {"source_type": "copilot_claims",
                   "source_ref": source_ref,
                   "time_start": exported_at,
                   "time_end": exported_at},
        "signals": [],
    }
    report = {"records": len(records), "kept": 0, "dropped": []}
    seen = set()
    for i, rec in enumerate(records):
        sig, why = claim_signal(rec, batch_id, i, bootstrap)
        if sig is None:
            report["dropped"].append({"index": i, "reason": why,
                                      "task": (rec.get("task") or "")[:60]})
            continue
        if sig["signal_id"] in seen:
            report["dropped"].append({"index": i, "reason": "duplicate",
                                      "task": sig["title"][:60]})
            continue
        seen.add(sig["signal_id"])
        batch["signals"].append(sig)
        report["kept"] += 1
    return batch, report


def _scan_json(text):
    """Every JSON array or object in the text, in order.

    A chat session produces one response per meeting, and a week of
    meetings gets pasted into one document -- so the file holds several
    arrays with prose between them, and json.loads fails on the second
    one with "Extra data: line 4 column 1", which names a position and
    explains nothing. Slicing from the first [ to the last ] does not
    help either: that span contains all of them and is not itself valid
    JSON.

    raw_decode reads ONE value and reports where it stopped, so this
    walks the file taking values and skipping whatever sits between
    them. One array, ten arrays, arrays with commentary in between, or a
    bare object per meeting all arrive the same way.
    """
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        starts = [x for x in (text.find("[", i), text.find("{", i))
                  if x != -1]
        if not starts:
            break
        s = min(starts)
        try:
            value, end = dec.raw_decode(text, s)
        except ValueError:
            i = s + 1          # not the start of a value after all
            continue
        # Only things that could BE claims. When one record is malformed
        # -- a truncated paste, a stray quote -- the array around it fails
        # to parse, this walks forward, and the next [ it meets is an
        # inner one: "sources": ["Scrum - Aug 28"]. Collected blindly,
        # that arrives downstream as the string "Scrum - Aug 28" being
        # asked for its .get("person"), which is a crash naming a type
        # rather than the mangled record that caused it.
        if isinstance(value, dict):
            out.append(value)
            i = end
        elif isinstance(value, list) and any(isinstance(x, dict)
                                             for x in value):
            out.append([x for x in value if isinstance(x, dict)])
            i = end
        else:
            i = s + 1          # a list of strings is not a claim list
    return out


def load_records(path):
    """Tolerant of how this arrives: pasted out of a chat window.

    Copilot wraps JSON in markdown fences, sometimes prefixes a sentence,
    and sometimes trails one -- and the file often arrives via Word, which
    curls every quote into a character JSON does not accept. Failing on any
    of that would be technically correct and practically useless, so the
    quotes are straightened and every JSON value in the file is collected
    -- the same tolerance the extraction worker applies to model
    responses, for the same reason.
    """
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    try:
        data = json.loads(text)
    except ValueError:
        # Word, Outlook and Teams all "helpfully" curl quotes on paste, and
        # JSON only accepts straight ones. The resulting error names a
        # column number and explains nothing, so normalise rather than make
        # someone find it by hand. Only on the fallback path: text that
        # already parses is never touched.
        text = (text.replace(u"\u201c", '"').replace(u"\u201d", '"')
                    .replace(u"\u2018", "'").replace(u"\u2019", "'"))
        values = _scan_json(text)
        if not values:
            raise SystemExit(
                "no JSON found in {0}. Paste the export as-is -- fences "
                "and surrounding prose are fine, but the claims "
                "themselves must be JSON.".format(path))
        data = []
        for v in values:
            if isinstance(v, list):
                data.extend(v)
            elif isinstance(v, dict):
                for key in ("claims", "items", "records", "data"):
                    if isinstance(v.get(key), list):
                        data.extend(v[key])
                        break
                else:
                    data.append(v)
        if len(values) > 1:
            print("   ({0} separate JSON blocks found and merged -- one "
                  "per response, by the look of it)".format(len(values)))
        bad = sum(1 for d in data if not isinstance(d, dict))
        if bad:
            print("   ({0} fragment(s) skipped -- not claim objects. A "
                  "malformed record usually means a truncated paste; the "
                  "rest still imports.)".format(bad))
            data = [d for d in data if isinstance(d, dict)]
        return data
    if isinstance(data, dict):
        for key in ("claims", "items", "records", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if not isinstance(data, list):
        raise SystemExit("expected a JSON array of claim objects")
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--dsn", default=os.environ.get("TL_COPILOT_DSN"),
                    help="where to write; defaults to TL_COPILOT_DSN")
    ap.add_argument("--label", default="export",
                    help="names the window, e.g. the week or the meeting set")
    ap.add_argument("--exported-at", default=None,
                    help="ISO timestamp of the export; defaults to now")
    ap.add_argument("--bootstrap", action="store_true",
                    help="first import into an empty ledger: map every "
                         "claim to a type that can create a task, so "
                         "progress updates about work the board has "
                         "never seen are not silently consumed")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    exported_at = args.exported_at or (
        datetime.datetime.now().replace(microsecond=0).isoformat())

    total = 0
    for path in args.files:
        records = load_records(path)
        batch, report = build_batch(records, exported_at, args.label,
                                    args.bootstrap)

        import jsonschema
        with open(os.path.join(ROOT, "contracts",
                               "extraction_schema.json")) as f:
            jsonschema.validate(batch, json.load(f))

        kinds = {}
        for s in batch["signals"]:
            kinds[s["type"]] = kinds.get(s["type"], 0) + 1
        people = sorted(set(c["person_ref"] for s in batch["signals"]
                            for c in s["owner_candidates"]))
        print("{0}: {1} record(s) -> {2} signal(s), {3} people".format(
            os.path.basename(path), report["records"], report["kept"],
            len(people)))
        print("   types: " + ", ".join(
            "{0}={1}".format(k, v) for k, v in sorted(kinds.items())))
        print("   every owner basis 'inferred', confidence capped at "
              "{0} -- these claims cannot be checked, so they must not "
              "outrank claims that can".format(MAX_CONFIDENCE))
        for d in report["dropped"]:
            print("   dropped [{0}]: {1}".format(d["reason"], d["task"]))

        if args.dry_run:
            print(json.dumps(batch["signals"][:1], indent=1)[:900])
            continue
        if not args.dsn:
            raise SystemExit("set TL_COPILOT_DSN or pass --dsn (or use "
                             "--dry-run)")
        import signal_store_pg
        fallback = sorted(set(c["person_ref"] for s in batch["signals"]
                              for c in s["owner_candidates"]))
        result = signal_store_pg.write_batch(args.dsn, batch, fallback)
        print("   db: {0} inserted, {1} already present, {2} pool event(s)"
              .format(result["signals_inserted"], result["already_present"],
                      result["pool_events"]))
        total += result["signals_inserted"]

    if total:
        print("\nwrote {0} signal(s). They are proposals-in-waiting like "
              "any other: nothing reaches a board without a person "
              "deciding.".format(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
