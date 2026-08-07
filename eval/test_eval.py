"""Tests for the attribution eval builder.

Fixtures here are invented. The real worksheets this code produces contain
colleague speech and live outside the repository -- which is itself one of
the things under test.

Run: python3 test_eval.py   (no database needed)
"""

import os
import sys

sys.path.insert(0, ".")
import attribution_eval as ev

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


INDEX = {
    "utt-1": {"speaker": "Sam", "speaker_ref": "aad-sam",
              "text": "I'll take the migration next sprint",
              "source_ref": "meeting-a"},
    "utt-2": {"speaker": "Robin", "speaker_ref": "aad-robin",
              "text": "Sam should own the migration",
              "source_ref": "meeting-a"},
    "utt-3": {"speaker": "Robin", "speaker_ref": "aad-robin",
              "text": "I'm picking up the dashboard work",
              "source_ref": "meeting-a"},
}

ROWS = [
    # Self-referential sentence, no owner extracted: the shape the live
    # rejections turned out to have.
    {"proposal_id": "p-1", "signal_id": "s-1", "title": "Do the migration",
     "description": None, "reject_reason": "wrong_owner",
     "decided_by": "aad-sam", "extracted_owner": None, "basis": None,
     "confidence": 0.7, "resource_ids": ["utt-1"]},
    # Third-party assignment: the speaker is NOT the owner.
    {"proposal_id": "p-2", "signal_id": "s-2", "title": "Do the migration",
     "description": None, "reject_reason": "wrong_owner",
     "decided_by": "aad-sam", "extracted_owner": "aad-robin",
     "basis": "self_reference", "confidence": 0.8,
     "resource_ids": ["utt-2"]},
    # The extractor named a third party when the speaker was talking about
    # their own work: the failure the speaker's identity fixes.
    {"proposal_id": "p-4", "signal_id": "s-4", "title": "Dashboard work",
     "description": None, "reject_reason": "wrong_owner",
     "decided_by": "aad-sam", "extracted_owner": "aad-devesh",
     "basis": "explicit_assignment", "confidence": 0.8,
     "resource_ids": ["utt-3"]},
    # A citation whose window is no longer on disk.
    {"proposal_id": "p-3", "signal_id": "s-3", "title": "Something else",
     "description": None, "reject_reason": "wrong_owner",
     "decided_by": "aad-sam", "extracted_owner": None, "basis": None,
     "confidence": 0.6, "resource_ids": ["utt-gone"]},
]


def test_build():
    print("building cases")
    cases = ev.build_cases(ROWS, INDEX)
    ok(len(cases) == 4, "one case per rejected proposal")
    c1 = cases[0]
    ok(c1["speaker_ref"] == "aad-sam" and c1["utterances"][0]["text"],
       "the sentence and its speaker are recovered from the inbox")
    ok(c1["self_referential"] is True,
       "a first-person sentence is flagged self-referential")
    ok(c1["correct_owner_ref"] is None,
       "the label is left blank: the ledger records why a proposal was "
       "rejected, never who was right")
    ok(cases[1]["self_referential"] is False,
       "'Sam should own it' is not self-referential")
    c3 = cases[3]
    ok(c3["utterances"][0]["text"] is None and "not found"
       in c3["utterances"][0]["note"],
       "a missing window is reported, not silently dropped")


def test_score():
    print("scoring")
    cases = ev.build_cases(ROWS, INDEX)
    sheet = ev.worksheet(cases)
    ok(sheet["counts"]["labelled"] == 0 and "Fill correct_owner_ref"
       in sheet["instructions"],
       "an unlabelled worksheet says what to do with it")
    blank = ev.score(sheet)
    ok(blank["labelled"] == 0 and "Nothing labelled" in blank["verdict"],
       "scoring an unlabelled sheet asks for labels rather than "
       "reporting zero accuracy")

    cases[0]["correct_owner_ref"] = "aad-sam"      # speaker owns it
    cases[1]["correct_owner_ref"] = "aad-sam"      # speaker assigns AWAY
    cases[2]["correct_owner_ref"] = "aad-robin"    # speaker owns it
    r = ev.score({"cases": cases})
    ok(r["labelled"] == 3 and r["speaker_would_fix"] == 2
       and r["speaker_is_not_owner"] == 1,
       "the score separates cases the speaker resolves from the ones a "
       "speaker-defaulting fix would BREAK")
    ok(r["self_referential_but_unattributed"] == 1,
       "it counts self-referential sentences the extractor left unowned -- "
       "the guard-shaped cases")
    ok(r["blamed_someone_other_than_the_speaker"] == 1,
       "and cases where the wrong person was named outright")
    ok("67%" in r["verdict"] and "prompt or guard" in r["verdict"],
       "the verdict quotes the measured share and says what it implies")
    high = ev.score({"cases": [dict(c, correct_owner_ref=c["speaker_ref"])
                               for c in cases if c.get("speaker_ref")]})
    ok("author_ref" in high["verdict"],
       "when the speaker resolves nearly all of them, the verdict names "
       "the fix: wire author_ref, do not rewrite the prompt")


def test_repo_guard():
    print("privacy guard")
    repo = os.path.abspath(".")
    for bad in (os.path.join(repo, "eval", "sheet.json"),
                os.path.join(repo, "sheet.json")):
        try:
            ev.assert_outside_repo(bad, repo)
            ok(False, "should have refused " + bad)
        except ValueError as e:
            ok("refusing to write real utterances" in str(e),
               "refuses to write a worksheet inside the repository")
    good = ev.assert_outside_repo("~/task-ledger-eval/sheet.json", repo)
    ok(os.path.isabs(good) and not good.startswith(repo + os.sep),
       "an outside path is accepted and made absolute")


def test_index():
    print("inbox index")
    ok(ev.index_inbox(["/nonexistent/window.json"]) == {},
       "an unreadable window is skipped, not fatal")


if __name__ == "__main__":
    test_build()
    test_score()
    test_repo_guard()
    test_index()
    print("\nall {0} eval checks passed".format(CHECKS[0]))
