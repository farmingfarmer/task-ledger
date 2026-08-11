"""Tests for the synthetic-meeting generator.

The cast in the real generator is read at runtime from a processed window,
so it never appears in this repository. The template below is invented for
exactly that reason -- it stands in for the shape of a real one.

Run: python3 test_synth.py   (no database, no LLM needed)
"""

import json
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, ".")
import synth_meetings as sm

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


TEMPLATE = {
    "job": {"source_type": "meeting_transcript", "source_ref": "fixture-1",
            "window_start": "2026-01-05T10:00:00Z",
            "window_end": "2026-01-05T10:30:00Z"},
    "directory": {"Robin Vale": "aad-robin", "Sam Okonjo": "aad-sam",
                  "Ida Pfeiffer": "aad-ida"},
    "items": [
        {"resource_id": "f-1", "author_name": "Robin Vale",
         "author_ref": "aad-robin", "timestamp": "2026-01-05T10:00:00Z",
         "text": "invented"},
        {"resource_id": "f-2", "author_name": "Sam Okonjo",
         "author_ref": "aad-sam", "timestamp": "2026-01-05T10:02:00Z",
         "text": "invented"},
    ],
}


def write_template(d):
    p = os.path.join(d, "template.json")
    with open(p, "w") as f:
        json.dump(TEMPLATE, f)
    return p


def generate(d, seed):
    """Run the generator the way the CLI does, returning (refs, windows)."""
    cast = sm.load_cast(write_template(d))
    import datetime
    day = datetime.date(2026, 2, 1)
    rng = random.Random(seed)
    out = []
    for i in range(3):
        ref, window, golden = sm.build_meeting(
            rng, cast, "meeting_transcript", day, i, 14, seed)
        out.append((ref, window, golden))
    return out


def test_seed_in_ids():
    print("seed collision")
    d = tempfile.mkdtemp()
    try:
        a = generate(d, 7)
        b = generate(d, 23)
        refs_a = set(r for r, _, _ in a)
        refs_b = set(r for r, _, _ in b)
        ok(not (refs_a & refs_b),
           "two seeds on the same day share no source_ref")
        # batch_id hashes source_ref + window bounds + prompt version, so
        # equal refs with equal bounds collide even when the words differ.
        bounds_a = set((r, w["job"]["window_start"], w["job"]["window_end"])
                       for r, w, _ in a)
        bounds_b = set((r, w["job"]["window_start"], w["job"]["window_end"])
                       for r, w, _ in b)
        ok(not (bounds_a & bounds_b),
           "nor the (ref, window_start, window_end) triple batch_id hashes")
        rids_a = set(i["resource_id"] for _, w, _ in a for i in w["items"])
        rids_b = set(i["resource_id"] for _, w, _ in b for i in w["items"])
        ok(not (rids_a & rids_b),
           "resource ids are disjoint too, so evidence cannot cross sets")
        ok(len(rids_a) == sum(len(w["items"]) for _, w, _ in a),
           "resource ids are unique within a set")
    finally:
        shutil.rmtree(d)


def test_determinism():
    print("determinism")
    d = tempfile.mkdtemp()
    try:
        first = generate(d, 7)
        second = generate(d, 7)
        ok(json.dumps(first) == json.dumps(second),
           "same seed reproduces windows and golden labels exactly")
    finally:
        shutil.rmtree(d)


def test_golden_labels():
    print("golden labels")
    d = tempfile.mkdtemp()
    try:
        rules = {"speaker": 0, "target": 0, "none": 0}
        for ref, window, golden in generate(d, 7):
            by_rid = dict((i["resource_id"], i) for i in window["items"])
            for c in golden["claims"]:
                rules[c["owner_rule"]] += 1
                item = by_rid[c["resource_id"]]
                if c["owner_rule"] == "speaker":
                    assert c["expected_owner_ref"] == item["author_ref"], ref
                elif c["owner_rule"] == "none":
                    assert c["expected_owner_ref"] is None, ref
                else:
                    assert c["expected_owner_ref"] != item["author_ref"], ref
        ok(rules["speaker"] and rules["target"] and rules["none"],
           "all three owner rules are planted: {0}".format(rules))
        ok(True, "speaker claims label the speaker, target claims someone "
                 "else, and none-claims label nobody")
    finally:
        shutil.rmtree(d)


def test_no_chatter_labelled():
    print("chatter")
    d = tempfile.mkdtemp()
    try:
        texts = set()
        for _, window, golden in generate(d, 7):
            labelled = set(c["resource_id"] for c in golden["claims"])
            for i in window["items"]:
                if i["resource_id"] not in labelled:
                    texts.add(i["text"])
        ok(all(t in [line[0].format(subj="x", target="Y")
                     for line in sm.LINES if line[1] == "chatter"]
               for t in texts) if texts else True,
           "unlabelled utterances are only chatter, never dropped claims")
    finally:
        shutil.rmtree(d)


def test_fictional_cast():
    print("invented cast")
    directory, speakers, source_type = sm.fictional_cast()
    ok(len(speakers) == 8 and source_type == "meeting_transcript",
       "eight invented people, in the shape load_cast returns")
    ok(all(r.startswith("aad-") for r in speakers),
       "refs look like directory refs, because that is what resolves")
    # Surname-only speakers are the case attribution has to survive, so the
    # invented directory has to carry the same aliases a real one does.
    ok(directory.get("Vale") == "aad-robin-vale"
       and directory.get("Robin") == "aad-robin-vale",
       "surname and first-name aliases both resolve")
    ok(directory.get("Pfeiffer-Roy") == "aad-ida-pfeiffer-roy"
       and directory.get("Vries") == "aad-marta-de-vries",
       "double-barrelled and two-word surnames resolve too -- the shapes "
       "a directory actually contains")

    d = tempfile.mkdtemp()
    try:
        rng = random.Random(11)
        import datetime
        cast = sm.fictional_cast()
        ref, window, golden = sm.build_meeting(
            rng, cast, "meeting_transcript", datetime.date(2026, 2, 1),
            0, 16, 11)
        names = set(i["author_name"] for i in window["items"])
        ok(names <= set(sm.FICTIONAL_CAST),
           "every speaker in a generated window is one of the invented "
           "people -- this material is committable only if that holds")
        ok(golden["claims"], "golden labels are planted as usual")
    finally:
        shutil.rmtree(d)


def test_repo_guard():
    print("privacy guard")
    try:
        sm.assert_outside_repo(sm.REPO)
        ok(False, "should have refused")
    except SystemExit:
        ok(True, "refuses to generate inside the repository")
    try:
        sm.assert_outside_repo(os.path.join(sm.REPO, "testdata", "out"))
        ok(False, "should have refused")
    except SystemExit:
        ok(True, "refuses a subdirectory of the repository too")
    d = tempfile.mkdtemp()
    try:
        ok(sm.assert_outside_repo(d) == os.path.abspath(d),
           "an outside path is accepted and made absolute")
    finally:
        shutil.rmtree(d)


def test_cast_from_template():
    print("cast")
    d = tempfile.mkdtemp()
    try:
        directory, speakers, source_type = sm.load_cast(write_template(d))
        ok(set(speakers) == set(["aad-robin", "aad-sam"]),
           "speakers come from who actually spoke, not the whole directory")
        ok(len(directory) == 3 and source_type == "meeting_transcript",
           "directory and source type carry over from the template")
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "synth_meetings.py")).read()
        # This file DOES hardcode ids now -- the invented cast, so the
        # material can be committed. The invariant that still matters is
        # that every one of them is invented: a real ref appearing here
        # would put a colleague's identity in the repository.
        import re as _re
        hardcoded = set(_re.findall(r'"(aad-[a-z0-9-]+)"', src))
        ok(hardcoded and hardcoded <= set(sm.FICTIONAL_CAST.values()),
           "every directory id in the source belongs to the invented cast "
           "({0} of them)".format(len(hardcoded)))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    test_seed_in_ids()
    test_determinism()
    test_golden_labels()
    test_no_chatter_labelled()
    test_fictional_cast()
    test_repo_guard()
    test_cast_from_template()
    print("\nall {0} synth generator checks passed".format(CHECKS[0]))
