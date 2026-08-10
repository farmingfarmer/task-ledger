"""Attribution eval: turn real rejections into a measurable test set.

Four out of five rejections on the first live run were `wrong_owner`. The
synthetic corpora in testdata/ measure attribution at 74-81%, but they
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
            # The fields a human fills in. no_owner_stated is not the same
            # as an empty correct_owner_ref: one says "the sentence names
            # nobody", the other says "not looked at yet", and conflating
            # them hides the most common case behind an unlabelled count.
            "correct_owner_ref": None,
            "no_owner_stated": False,
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
    # A sentence that names nobody is a LABELLED case with a real finding:
    # it should never have produced a per-person proposal at all.
    no_owner = [c for c in cases if c.get("no_owner_stated")
                and not c.get("correct_owner_ref")]
    unlabelled = len(cases) - len(labelled) - len(no_owner)

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
        "no_owner_stated": len(no_owner),
        "speaker_would_fix": len(speaker_would_fix),
        "speaker_would_fix_pct": round(100.0 * len(speaker_would_fix) / n, 1)
                                 if n else 0.0,
        "speaker_is_not_owner": len(speaker_wrong),
        "no_speaker_recorded": len(no_speaker),
        "blamed_someone_other_than_the_speaker": len(blamed_listener),
        "by_extracted_basis": by_basis,
        "self_referential_but_unattributed": len(self_ref_no_owner),
        "verdict": _verdict(n, len(speaker_would_fix), len(no_owner),
                            len(cases)),
    }


def _verdict(n, fixed, no_owner=0, total=0):
    lead = ""
    if no_owner and total:
        lead = ("{0} of {1} cases name NOBODY: those must not become any "
                "individual's proposal, which is a routing question, not an "
                "attribution one. Of the rest: ").format(no_owner, total)
    if not n:
        return lead + ("nothing else labelled -- fill correct_owner_ref and "
                       "run score again.")
    pct = 100.0 * fixed / n
    if pct >= 70:
        return lead + ("speaker identity alone resolves {0:.0f}% of them. "
                       "The fix "
                       "is to feed author_ref into attribution, not to "
                       "rewrite the prompt.".format(pct))
    if pct >= 30:
        return lead + ("speaker identity resolves {0:.0f}%. Worth wiring, "
                       "but a "
                "prompt or guard change is needed for the rest -- look at "
                "by_extracted_basis for which ladder rung is "
                "failing.".format(pct))
    return lead + ("speaker identity resolves only {0:.0f}%. The failure is in "
            "reading the sentence, not in knowing who spoke: this is prompt "
            "and guard work.".format(pct))


def score_golden(windows):
    """Score attribution against planted labels.

    The worksheet measures the extractor against a human's reading of real
    sentences, which is the truth that matters and costs a labelling pass
    every time. This measures it against synthetic meetings whose owners
    were decided before the model saw them, so the loop can run on every
    change without anyone reading anything.

    windows: [{golden: {claims:[...]}, signals: [{author_ref,
    owner_candidates, evidence_resource_ids}]}]
    """
    out = {"claims": 0, "extracted": 0, "owner_right": 0, "owner_wrong": 0,
           "owner_missing": 0, "correctly_unowned": 0,
           "wrongly_owned": 0, "by_rule": {}, "misses": []}
    for w in windows:
        by_rid = {}
        for sig in w.get("signals") or []:
            for rid in sig.get("evidence_resource_ids") or []:
                by_rid.setdefault(rid, sig)
        for claim in (w.get("golden") or {}).get("claims") or []:
            out["claims"] += 1
            rule = claim.get("owner_rule") or "unknown"
            slot = out["by_rule"].setdefault(
                rule, {"n": 0, "right": 0, "wrong": 0, "missing": 0})
            slot["n"] += 1
            sig = by_rid.get(claim["resource_id"])
            if sig is None:
                # Extraction never produced anything for this sentence.
                # That is the most actionable miss of all, so it carries
                # its text like the others rather than being a bare count.
                slot["missing"] += 1
                out["owner_missing"] += 1
                out["misses"].append({"text": claim.get("text"),
                                      "want": claim.get("expected_owner_ref"),
                                      "got": None, "why": "not extracted"})
                continue
            out["extracted"] += 1
            got = ((sig.get("owner_candidates") or [{}])[0] or {}).get(
                "person_ref")
            want = claim.get("expected_owner_ref")
            if want is None:
                # The sentence names nobody: leaving it unowned is right,
                # and naming somebody is the failure the live run showed.
                if got is None:
                    out["correctly_unowned"] += 1
                    slot["right"] += 1
                else:
                    out["wrongly_owned"] += 1
                    slot["wrong"] += 1
                    out["misses"].append({"text": claim.get("text"),
                                          "want": None, "got": got})
            elif got == want:
                out["owner_right"] += 1
                slot["right"] += 1
            elif got is None:
                out["owner_missing"] += 1
                slot["missing"] += 1
                out["misses"].append({"text": claim.get("text"),
                                      "want": want, "got": None})
            else:
                out["owner_wrong"] += 1
                slot["wrong"] += 1
                out["misses"].append({"text": claim.get("text"),
                                      "want": want, "got": got})
    graded = out["owner_right"] + out["correctly_unowned"]
    out["accuracy"] = (round(100.0 * graded / out["claims"], 1)
                       if out["claims"] else 0.0)
    return out


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
