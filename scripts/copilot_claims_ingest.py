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

2. Confidence is CAPPED unless the claim is ANCHORED, at levels chosen
   against the op floors rather than by feel. Copilot's "high" is its own
   uncalibrated word. The cap sits above the floors for create/status/
   blocker -- those must reach a human, that is the point -- and below the
   0.80 and 0.85 floors for complete, cancel and merge, so an unverifiable
   claim is structurally incapable of proposing that work is finished or
   thrown away. When the exported quote is FOUND in a transcript on disk
   the cap lifts (ANCHORED_CONFIDENCE), because the thing it was
   protecting against -- a claim nobody can check outranking one they can
   -- no longer applies to that claim.

3. Owner basis is always 'inferred'. explicit_assignment and
   self_reference mean somebody was heard saying something, and nothing
   here was heard by this system. Nothing is lost: the assistant's own
   claim_type, evidence_strength and self-rated confidence are written into
   the signal's description, where the person deciding actually reads them
   -- burying them in a raw blob would hide the unverifiability from the
   one person who needs it. They are simply not promoted to a certainty
   they have not got.

"project" becomes the signal's parent_hint: a NAME, carried to the review
card and resolved into a real parent only when a person accepts a card
that shows it. Filing work under a parent an assistant picked would put a
task on somebody's board that nobody agreed to, which is the one thing
this system does not do.

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

# ANCHORED claims -- the quote was found in a transcript this system
# holds, so the citation is checkable and the reason for capping is gone.
# The cap was never about Copilot being untrustworthy in general; it was
# about a claim nobody could verify not being allowed to outrank one that
# could. A matched claim IS one that can.
#
# 'high' sits above the closure floors (complete_task and cancel_task at
# 0.80/0.85) on purpose: a verified completion claim SHOULD be able to
# propose that work is finished. That is the whole payoff, and it is
# still only a proposal -- the closure gate continues to demand the
# speaker be the owner or the claim corroborated, and no board moves
# without a person. 'medium' and 'low' stay under those floors, because
# an assistant hedging about a sentence it did find is different from an
# assistant certain about one.
ANCHORED_CONFIDENCE = {"high": 0.88, "medium": 0.70, "low": 0.50}

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


def claim_signal(rec, batch_id, idx, bootstrap=False, transcripts=None):
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
        return None, "not a claim object", None
    person = (rec.get("person") or "").strip()
    task = (rec.get("task") or "").strip()
    if not task:
        return None, "no task text", None
    if not person:
        # Everything here is per-person by construction; a record without
        # one is malformed, and guessing an owner is the failure this
        # system exists to avoid.
        return None, "no person named", None

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

    said = (rec.get("confidence") or "").strip().lower()
    conf = CONFIDENCE.get(said, 0.40)

    # ANCHORING. The cap and the 'inferred' basis exist for exactly one
    # reason: Copilot cites a meeting, not a sentence, so nobody can check
    # it. When the export carries the quote AND that sentence is found in
    # a transcript already on disk, that reason is gone -- the claim gets
    # the same evidence ref the local extractor would have written, the
    # same content hash, and the speaker from the transport rather than
    # from the model. It is then a claim about a sentence somebody said,
    # which is what every other signal in this system is, so it is capped
    # no further and marked no differently.
    anchor, how = (None, None)
    quote = (rec.get("quote") or "").strip()
    if transcripts is not None and quote:
        anchor, how = transcripts.find(quote)
    if anchor is None:
        conf = min(conf, MAX_CONFIDENCE)
    else:
        conf = ANCHORED_CONFIDENCE.get(said, conf)

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
    if anchor is not None:
        asserted = ("Copilot: {0}, self-rated {1}. Quote matched to the "
                    "transcript ({2}) -- {3}, so this cites a sentence "
                    "like any other signal.").format(
                        rec.get("claim_type") or "unspecified",
                        rec.get("confidence") or "unstated",
                        how, anchor["resource_id"])
    else:
        asserted = "Copilot: {0}, evidence {1} (UNVERIFIED - no retrievable " \
                   "source), self-rated {2}{3}".format(
                       rec.get("claim_type") or "unspecified",
                       rec.get("evidence_strength") or "unstated",
                       rec.get("confidence") or "unstated",
                       (", sources: " + "; ".join(sources)) if sources else "")
    # The estimate rides IN the description, as spoken -- "Estimate: 3
    # points" -- so the reviewer sees it on the card, it travels into
    # the task's description on accept, and the ledger's deterministic
    # parser can find it there and convert at the CURRENT policy rate.
    # Deliberately not a converted number and not a hidden field: words
    # a person said, kept where people read.
    est = (rec.get("estimate") or "").strip()
    description = " ".join(x for x in [
        ("Estimate: " + est + ".") if est and est.lower() not in
            ("null", "none") else None,
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
        # The speaker comes from the TRANSCRIPT when the quote matched --
        # a transport fact, the same place the local extractor takes it
        # from -- and is None otherwise, because without a matched
        # sentence nobody was heard. said_by is deliberately not trusted
        # on its own: it is the model's account of who spoke.
        "author_ref": anchor["author_ref"] if anchor else None,
        "owner_candidates": [{
            "person_ref": ref_for(person),
            # Still 'inferred': the quote proves the sentence was said,
            # not that the assistant read the ownership out of it right.
            "basis": "inferred",
            "confidence": conf,
        }],
        "deadline": None,
        # PROJECT -> parent hint. Copilot groups claims under a workstream
        # name, and this is the field that makes a fragment legible:
        # "count events per day" is noise on its own and exactly right
        # under the stroke prediction work. It stays a NAME here on
        # purpose -- resolving it to a task id at ingest would file work
        # under a parent nobody chose, on the word of an assistant. The
        # name rides to the card, the card says it, and accepting is what
        # files it.
        "parent_hint": ((rec.get("project") or "").strip() or None)
        if (rec.get("project") or "").strip().lower() not in
           ("", "null", "none", "n/a") else None,
        "blocker": ({"description": "; ".join(blockers)[:300],
                     "blocking_party_ref": None} if blockers else None),
        "evidence": [{
            "source_type": ("transcript_utterance" if anchor
                            else "assistant_claim"),
            "resource_id": (anchor["resource_id"] if anchor
                            else resource_id),
            "span": None,
            # Anchored: the hash of the utterance, byte-identical to what
            # the extraction worker writes, so the receipt verifies the
            # same way whichever path produced it. Unanchored: the hash of
            # the exported record, which cannot prove what anyone said but
            # does prove the export was not edited afterwards.
            "content_hash": (anchor["content_hash"] if anchor
                             else sha(json.dumps(rec, sort_keys=True))),
        }],
    }
    return signal, None, (how if anchor else None)


_SRC_DATE = re.compile(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?")


def _meeting_date(source, exported_at):
    """A date for the meeting a claim cites, parsed from its sources
    string when one is written there ('Weekly Sprint 10/07'), the
    export date otherwise. Yearless dates land in the PAST, same rule
    as status reports: a claims export describes meetings that
    happened."""
    m = _SRC_DATE.search(source or "")
    if not m:
        return exported_at[:10]
    import datetime as _dt
    today = _dt.date.fromisoformat(exported_at[:10])
    month, day = int(m.group(1)), int(m.group(2))
    try:
        if m.group(3):
            y = int(m.group(3))
            d = _dt.date(y + 2000 if y < 100 else y, month, day)
        else:
            d = _dt.date(today.year, month, day)
            if d > today + _dt.timedelta(days=7):
                d = _dt.date(today.year - 1, month, day)
    except ValueError:
        return exported_at[:10]
    return d.isoformat()


def build_batch(records, exported_at, label, bootstrap=False,
                transcripts=None):
    """One batch PER MEETING, grouped by each claim's own sources
    field -- not one batch per export. The export-shaped version put a
    month of meetings into a single "conversation" stamped with the day
    the operator ran the import: the first pilot reviewer opened his
    Conversations panel and reasonably concluded everything before that
    date had vanished. The per-meeting identity was in every claim all
    along; now it is used. Returns (batches, report)."""
    groups, order = {}, []
    for i, rec in enumerate(records):
        src = ((rec.get("sources") or ["export"])[0]
               if isinstance(rec, dict) else "export") or "export"
        if src not in groups:
            groups[src] = []
            order.append(src)
        groups[src].append((i, rec))

    batches = []
    report = {"records": len(records), "kept": 0, "dropped": [],
              "anchored": 0, "anchor_how": {}, "meetings": len(order)}
    for src in order:
        met = _meeting_date(src, exported_at)
        source_ref = "copilot-{0}-{1}".format(met, slug(src, 32))
        batch_id = sha(source_ref, PROMPT_VERSION)[:32]
        batch = {
            "batch_id": batch_id,
            "extracted_at": exported_at,
            "extractor": {"model": "copilot:claims-export",
                          "prompt_version": PROMPT_VERSION},
            "window": {"source_type": "copilot_claims",
                       "source_ref": source_ref,
                       "time_start": met + "T00:00:00",
                       "time_end": met + "T23:59:00"},
            "signals": [],
        }
        seen = set()
        for i, rec in groups[src]:
            sig, why, how = claim_signal(rec, batch_id, i, bootstrap,
                                         transcripts)
            if how:
                report["anchored"] += 1
                kind = how.split()[0]      # exact / contained / fuzzy
                report["anchor_how"][kind] = (
                    report["anchor_how"].get(kind, 0) + 1)
            if sig is None:
                report["dropped"].append(
                    {"index": i, "reason": why,
                     "task": (rec.get("task") or "")[:60]
                     if isinstance(rec, dict) else ""})
                continue
            if sig["signal_id"] in seen:
                report["dropped"].append({"index": i,
                                          "reason": "duplicate",
                                          "task": sig["title"][:60]})
                continue
            seen.add(sig["signal_id"])
            batch["signals"].append(sig)
            report["kept"] += 1
        if batch["signals"]:
            batches.append(batch)
    return batches, report


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
    ap.add_argument("--no-anchor", action="store_true",
                    help="do not try to match quotes to transcripts; every "
                         "claim stays unverified and capped")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    exported_at = args.exported_at or (
        datetime.datetime.now().replace(microsecond=0).isoformat())

    total = 0
    for path in args.files:
        records = load_records(path)
        # The transcripts the ledger already holds. A quote that matches
        # one of them turns an unverifiable claim into a cited one.
        transcripts = None
        if not args.no_anchor:
            sys.path.insert(0, os.path.join(ROOT, "extraction"))
            import quote_anchor
            inbox = os.path.expanduser(
                os.environ.get("TL_INBOX", "~/task-ledger-inbox"))
            transcripts = quote_anchor.Transcripts(inbox)
            if not len(transcripts):
                print("   (no transcripts found in {0} -- claims will stay "
                      "unverified)".format(inbox))
                transcripts = None
        batches, report = build_batch(records, exported_at, args.label,
                                      args.bootstrap, transcripts)

        import jsonschema
        with open(os.path.join(ROOT, "contracts",
                               "extraction_schema.json")) as f:
            _schema = json.load(f)
        for b in batches:
            jsonschema.validate(b, _schema)

        all_signals = [s for b in batches for s in b["signals"]]
        kinds = {}
        for s in all_signals:
            kinds[s["type"]] = kinds.get(s["type"], 0) + 1
        people = sorted(set(c["person_ref"] for s in all_signals
                            for c in s["owner_candidates"]))
        print("{0}: {1} record(s) -> {2} signal(s) across {3} "
              "meeting(s), {4} people".format(
                  os.path.basename(path), report["records"],
                  report["kept"], report["meetings"], len(people)))
        for b in batches:
            print("   {0}: {1} claim(s)".format(
                b["window"]["source_ref"], len(b["signals"])))
        print("   types: " + ", ".join(
            "{0}={1}".format(k, v) for k, v in sorted(kinds.items())))
        n_anchored = report["anchored"]
        if n_anchored:
            how = ", ".join("{0} {1}".format(v, k) for k, v
                            in sorted(report["anchor_how"].items()))
            print("   {0} of {1} claim(s) matched a sentence in your "
                  "transcripts ({2}) -- those cite an utterance, carry the "
                  "speaker, and are NOT capped".format(
                      n_anchored, report["kept"], how))
        if report["kept"] - n_anchored:
            print("   {0} unmatched: owner basis 'inferred', confidence "
                  "capped at {1} -- claims nobody can check must not "
                  "outrank claims that can".format(
                      report["kept"] - n_anchored, MAX_CONFIDENCE))
        for d in report["dropped"]:
            print("   dropped [{0}]: {1}".format(d["reason"], d["task"]))

        if args.dry_run:
            if all_signals:
                print(json.dumps(all_signals[:1], indent=1)[:900])
            continue
        if not args.dsn:
            raise SystemExit("set TL_COPILOT_DSN or pass --dsn (or use "
                             "--dry-run)")
        import signal_store_pg
        for b in batches:
            fallback = sorted(set(c["person_ref"]
                                  for s in b["signals"]
                                  for c in s["owner_candidates"]))
            result = signal_store_pg.write_batch(args.dsn, b, fallback)
            print("   db[{0}]: {1} inserted, {2} already present, {3} "
                  "pool event(s)".format(
                      b["window"]["source_ref"],
                      result["signals_inserted"],
                      result["already_present"], result["pool_events"]))
            total += result["signals_inserted"]

    if total:
        print("\nwrote {0} signal(s). They are proposals-in-waiting like "
              "any other: nothing reaches a board without a person "
              "deciding.".format(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
