"""Attribution eval: turn real rejections into a measurable test set.

Four out of five rejections on the first live run were `wrong_owner`. The
synthetic corpora in testdata/ measure attribution at 75-83%, but they
cannot produce the sentences that actually defeat it -- real people saying
"I'll pick that up" three turns after someone else's name. These
rejections can.

What the ledger records and what it does not:

  RECORDED   that a proposal was rejected, and that the reason was
             wrong_owner: the taxonomy captures the failure class.
  NOT        what the right answer was. The reject menu asks why, not
             who, so there is no ground truth in the database and no
             amount of querying will produce one.

So the builder emits a WORKSHEET -- every failing case with the sentence
that produced it, the speaker who said it, and who the extractor blamed --
with `correct_owner_ref` left blank for a human to fill. Twenty-two rows
is a ten-minute pass, and it is the only step a person has to do.

It also records `speaker_ref` per case, which lets the scorer answer the
question the fix depends on: how many of these would the speaker's own
identity have resolved? That is the hypothesis worth testing before
touching a prompt, and it costs nothing to measure.

THE TEXT IS NOT IN THE LEDGER, by design -- evidence is pointers plus
hashes. It comes from the processed inbox windows, joined on resource_id.
That also means a worksheet holds real colleague speech, so it never goes
near the repository: see assert_outside_repo().

Pure functions here; the CLI and the database live in
run_attribution_eval.py. Python 3.8 compatible. Stdlib only.
"""

import glob
import json
import os
import re

# "I'll pick that up", "we should do X" -- a sentence that attributes
# itself to whoever said it. When the extractor returns no owner for one
# of these, the speaker's own ref is the answer, and it was in the window
# all along.
FIRST_PERSON = re.compile(
    r"\b(i|i'?ll|i'?m|i'?ve|i'?d|my|mine|me|we|we'?ll|we'?ve|our|us)\b",
    re.I)


def index_inbox(paths):
    """resource_id -> {speaker, speaker_ref, text, source_ref}.

    A long meeting is ingested as several windows and the same utterance
    can appear in more than one; first one wins, since they are copies.
    """
    index = {}
    for path in paths:
        try:
            with open(path) as f:
                spec = json.load(f)
        except (ValueError, IOError):
            continue
        source_ref = (spec.get("job") or {}).get("source_ref") or ""
        for item in spec.get("items") or []:
            rid = item.get("resource_id")
            if not rid or rid in index:
                continue
            index[rid] = {"speaker": item.get("author_name"),
                          "speaker_ref": item.get("author_ref"),
                          "text": item.get("text"),
                          "source_ref": source_ref}
    return index


def inbox_paths(inbox):
    return sorted(glob.glob(os.path.join(inbox, "*.json")) +
                  glob.glob(os.path.join(inbox, "processed", "*.json")))


def build_cases(rows, index):
    """rows: one per rejected proposal, joined to its signals and evidence.

    Each row: {proposal_id, signal_id, title, description, reject_reason,
    decided_by, extracted_owner, basis, confidence, resource_ids[]}.
    """
    cases = []
    for r in rows:
        utterances = []
        speakers = []
        for rid in r.get("resource_ids") or []:
            hit = index.get(rid)
            if not hit:
                utterances.append({"resource_id": rid, "text": None,
                                   "speaker": None, "speaker_ref": None,
                                   "note": "utterance not found in the "
                                           "inbox: window may be archived"})
                continue
            utterances.append({"resource_id": rid, "text": hit["text"],
                               "speaker": hit["speaker"],
                               "speaker_ref": hit["speaker_ref"],
                               "first_person": bool(FIRST_PERSON.search(
                                   hit["text"] or ""))})
            if hit["speaker_ref"]:
                speakers.append(hit["speaker_ref"])
        cases.append({
            "case_id": r["proposal_id"],
            "signal_id": r.get("signal_id"),
            "source_ref": (index.get((r.get("resource_ids") or [None])[0])
                           or {}).get("source_ref"),
            "title": r.get("title"),
            "description": r.get("description"),
            "reject_reason": r.get("reject_reason"),
            "rejected_by": r.get("decided_by"),
            "extracted_owner_ref": r.get("extracted_owner"),
            "extracted_basis": r.get("basis"),
            "extraction_confidence": r.get("confidence"),
            # The speaker of the primary cited utterance: the input the
            # extractor did not have when these were produced.
            "speaker_ref": speakers[0] if speakers else None,
            # Whether the cited sentence attributes itself to its speaker.
            "self_referential": any(u.get("first_person")
                                    for u in utterances),
            "utterances": utterances,
            # The one field a human fills in.
            "correct_owner_ref": None,
            "label_note": None,
        })
    return cases


def worksheet(cases, meta=None):
    labelled = len([c for c in cases if c.get("correct_owner_ref")])
    return {"kind": "attribution-eval-worksheet", "version": 1,
            "meta": dict(meta or {}),
            "counts": {"cases": len(cases), "labelled": labelled},
            "instructions":
                "Fill correct_owner_ref on each case: the person the work "
                "actually belongs to, as an aad- ref. Leave it null if the "
                "sentence genuinely does not say. Then run: "
                "run_attribution_eval.py score --worksheet <this file>",
            "cases": cases}


def score(sheet):
    """What the labelled set says -- above all, whether the speaker's own
    identity would have resolved these."""
    cases = sheet.get("cases") or []
    labelled = [c for c in cases if c.get("correct_owner_ref")]
    unlabelled = len(cases) - len(labelled)

    speaker_would_fix = [c for c in labelled
                         if c.get("speaker_ref")
                         and c["correct_owner_ref"] == c["speaker_ref"]]
    speaker_wrong = [c for c in labelled
                     if c.get("speaker_ref")
                     and c["correct_owner_ref"] != c["speaker_ref"]]
    no_speaker = [c for c in labelled if not c.get("speaker_ref")]
    # Cases where the extractor blamed the person whose pool it landed in
    # rather than the person who spoke: the shape of the failure.
    blamed_listener = [c for c in labelled
                       if c.get("speaker_ref")
                       and c.get("extracted_owner_ref")
                       and c["extracted_owner_ref"] != c["speaker_ref"]
                       and c["correct_owner_ref"] == c["speaker_ref"]]
    self_ref_no_owner = [c for c in labelled
                         if c.get("self_referential")
                         and not c.get("extracted_owner_ref")]
    by_basis = {}
    for c in labelled:
        b = c.get("extracted_basis") or "unknown"
        by_basis[b] = by_basis.get(b, 0) + 1

    n = len(labelled)
    return {
        "cases": len(cases), "labelled": n, "unlabelled": unlabelled,
        "speaker_would_fix": len(speaker_would_fix),
        "speaker_would_fix_pct": round(100.0 * len(speaker_would_fix) / n, 1)
                                 if n else 0.0,
        "speaker_is_not_owner": len(speaker_wrong),
        "no_speaker_recorded": len(no_speaker),
        "blamed_someone_other_than_the_speaker": len(blamed_listener),
        "by_extracted_basis": by_basis,
        "self_referential_but_unattributed": len(self_ref_no_owner),
        "verdict": _verdict(n, len(speaker_would_fix)),
    }


def _verdict(n, fixed):
    if not n:
        return ("Nothing labelled yet -- fill correct_owner_ref and run "
                "score again.")
    pct = 100.0 * fixed / n
    if pct >= 70:
        return ("Speaker identity alone resolves {0:.0f}% of these. The fix "
                "is to feed author_ref into attribution, not to rewrite the "
                "prompt.".format(pct))
    if pct >= 30:
        return ("Speaker identity resolves {0:.0f}%. Worth wiring, but a "
                "prompt or guard change is needed for the rest -- look at "
                "by_extracted_basis for which ladder rung is "
                "failing.".format(pct))
    return ("Speaker identity resolves only {0:.0f}%. The failure is in "
            "reading the sentence, not in knowing who spoke: this is prompt "
            "and guard work.".format(pct))


def assert_outside_repo(path, repo_root):
    """A worksheet holds real colleague speech. Refuse to write one inside
    the working tree, where a later `git add -A` would sweep it up -- the
    rule is easier to enforce than to remember."""
    full = os.path.abspath(os.path.expanduser(path))
    root = os.path.abspath(repo_root)
    if full == root or full.startswith(root + os.sep):
        raise ValueError(
            "refusing to write real utterances inside the repository "
            "({0}). Choose a path outside it, e.g. ~/task-ledger-eval/"
            .format(full))
    return full
