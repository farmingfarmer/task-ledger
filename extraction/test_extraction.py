"""Tests for the extraction worker and Postgres signal store.

Fake-LLM tests cover the golden path, salvage of every malformed-signal
class, deterministic ids across re-runs, hallucinated-date rejection, name
resolution, and evidence hashing. The final section writes a batch to the
live taskintel database and asserts idempotency and pooling -- it runs only
if the DB is reachable, and reports itself skipped otherwise.

Run: python3 test_extraction.py
"""

import copy
import json
import os
import sys

sys.path.insert(0, ".")
import extraction_worker
import extraction_prompts

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


with open("sample_transcript.json") as f:
    SPEC = json.load(f)

GOLDEN = [
    {"type": "status_update",
     "title": "Deploy drift monitor to production",
     "description": "Staging deploy done; watching before prod",
     "speaker": "Alex",
     "owner_candidates": [{"name": "Alex", "basis": "self_reference",
                           "confidence": 0.9}],
     "status_signal": "in_progress", "deadline": None, "blocker": None,
     "confidence": 0.85, "evidence_items": [1]},
    {"type": "blocker",
     "title": "Fargate IAM role pending with cloud team",
     "speaker": "Priya",
     "owner_candidates": [{"name": "Priya", "basis": "inferred",
                           "confidence": 0.6}],
     "status_signal": "blocked",
     "deadline": None,
     "blocker": {"description": "IAM role for Fargate stuck with cloud team",
                 "blocking_party": None},
     "confidence": 0.8, "evidence_items": [2]},
    {"type": "completion_claim",
     "title": "Fix intake OCR crash",
     "speaker": "Dev",
     "owner_candidates": [{"name": "Dev", "basis": "self_reference",
                           "confidence": 0.95}],
     "status_signal": "completed", "deadline": None, "blocker": None,
     "confidence": 0.9, "evidence_items": [3]},
    {"type": "assignment",
     "title": "Wire up retraining pipeline alerting",
     "speaker": "Alex",
     "owner_candidates": [{"name": "Dev", "basis": "explicit_assignment",
                           "confidence": 0.9}],
     "status_signal": None,
     "deadline": {"raw_text": "end of next week", "precision": "week",
                  "date": "2026-07-31"},
     "blocker": None,
     "confidence": 0.85, "evidence_items": [4, 5]},
    {"type": "proposal",
     "title": "Rebuild feature store docs",
     "speaker": "Priya",
     "owner_candidates": [],
     "status_signal": "proposed", "deadline": None, "blocker": None,
     "confidence": 0.6, "evidence_items": [6]},
]

MALFORMED = [
    {"type": "vibes", "title": "x", "confidence": 0.9,
     "evidence_items": [1]},                                # bad type
    {"type": "task_mention", "title": "", "confidence": 0.9,
     "evidence_items": [1]},                                # empty title
    {"type": "task_mention", "title": "no evidence cited",
     "confidence": 0.9, "evidence_items": []},              # no evidence
    {"type": "task_mention", "title": "cites nonsense",
     "confidence": 0.9, "evidence_items": [99]},            # bad citation
    {"type": "task_mention", "title": "no confidence",
     "evidence_items": [1]},                                # missing conf
    "not even an object",                                   # wrong shape
]


def make_worker(response_payload, wrap=None):
    text = json.dumps(response_payload)
    if wrap == "fenced":
        text = "```json\n" + text + "\n```"
    elif wrap == "prose":
        text = "Here are the signals I found:\n" + text + "\nHope that helps!"

    calls = []

    def llm(messages):
        calls.append(messages)
        return text

    w = extraction_worker.ExtractionWorker(
        fetcher=lambda job: SPEC["items"],
        llm=llm,
        directory=SPEC["directory"],
        model_name="fake:test")
    return w, calls


def test_transcript_encodings():
    print("transcript encodings")
    import tempfile
    import textio
    body = ("Standup\nMonday, August 17, 2026 9:00 AM - 9:30 AM\n"
            "AI-generated content may be incorrect\n\n"
            "Marunycz, Alexander 0 minutes 10 seconds\n"
            "Working on the synth kit project \u2014 caf\u00e9 notes.\n")
    tmp = tempfile.mkdtemp()
    # Every encoding a Windows exporter actually produces. UTF-16 LE is
    # what Notepad calls "Unicode", and reading it as UTF-8 fails on byte
    # zero with a message that names the byte and not the cause.
    for enc in ("utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-32"):
        path = os.path.join(tmp, "t-{0}.txt".format(enc))
        with open(path, "w", encoding=enc) as f:
            f.write(body)
        got = textio.read_text(path)
        # Written with the platform's own newline convention on purpose:
        # on Windows that file really contains \r\n, exactly like a real
        # exporter's, and read_text must hand back \n regardless. This
        # assertion failed the first time the suite ever ran on Windows
        # -- the old read_text preserved the \r, gluing an invisible
        # byte to every utterance a parser split on \n.
        ok(got.replace("\ufeff", "") == body,
           "{0} reads back with accents, dashes and newlines "
           "intact".format(enc))
    # No BOM and not valid UTF-8: cp1252 is Windows' default and cannot
    # fail, so this degrades to readable text rather than an exception.
    path = os.path.join(tmp, "t-cp1252.txt")
    with open(path, "wb") as f:
        f.write("caf\u00e9 notes".encode("cp1252"))
    ok(textio.read_text(path) == "caf\u00e9 notes",
       "a BOM-less cp1252 file is read as cp1252, not thrown away")
    path = os.path.join(tmp, "t-crlf.txt")
    with open(path, "wb") as f:
        f.write(b"line one\r\nline two\rline three\n")
    ok(textio.read_text(path) == "line one\nline two\nline three\n",
       "\\r\\n and bare \\r both come back as \\n on every platform, "
       "so a hash or a quote computed from a transcript is the same "
       "hash on the machine that wrote it and the machine that reads it")
    enc, why = textio.detect_encoding(b"\xff\xfe\x00\x00hi")
    ok(enc == "utf-32",
       "UTF-32 LE is not mistaken for UTF-16 LE: it shares its first two "
       "bytes, so order of checking is the whole correctness")


def test_copilot_paste_shapes():
    print("copilot paste shapes")
    import tempfile
    sys.path.insert(0, os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "..", "scripts"))
    import copilot_claims_ingest as ci
    rec = ('{"person": "A B", "task": "t", "status": "open", '
           '"claim_type": "mention", "confidence": "high", '
           '"blockers": [], "sources": []}')
    tmp = tempfile.mkdtemp()

    def load(text):
        path = os.path.join(tmp, "c.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return ci.load_records(path)

    # A week of meetings is a week of chat responses, pasted into one
    # document. json.loads fails on the second array with "Extra data:
    # line 4 column 1", and slicing first-[ to last-] spans them all and
    # is not valid JSON either. Observed on a real export.
    ok(len(load("[%s]" % rec)) == 1, "one array, as documented")
    ok(len(load("First:\n[%s]\nSecond:\n[%s]" % (rec, rec))) == 2,
       "several responses pasted together are merged, not rejected")
    ok(len(load("```json\n[%s]\n```\nnote\n```json\n[%s]\n```"
                % (rec, rec))) == 2,
       "and the markdown fences a chat window adds do not matter")
    ok(len(load("%s\n\n%s" % (rec, rec))) == 2,
       "bare objects with no array around them still load")
    ok(len(load('{"claims": [%s]}' % rec)) == 1,
       "a wrapper object is unwrapped")
    ok(len(load(("[%s]" % rec).replace('"', "\u201c", 1))) == 1,
       "and a quote curled by Word does not cost the whole file")

    # One malformed record makes its whole array unparseable, the scan
    # walks forward, and the next [ it meets is an INNER one -- a
    # "sources" or "blockers" list. Collected blindly those arrive as
    # bare strings and crash on .get("person"), which names a type
    # instead of the mangled record that caused it.
    torn = ('[{"person": "A B", "task": "good", "status": "open", '
            '"claim_type": "mention", "confidence": "high", '
            '"blockers": [], "sources": ["Scrum - Aug 28"]},'
            ' {"person": "C D", "task": "truncated here, '
            '"sources": ["Scrum - Aug 28"], "blockers": ["needs data"]}]')
    got = load(torn)
    ok(len(got) == 1 and all(isinstance(g, dict) for g in got),
       "a truncated record costs that record, not the import -- and "
       "never leaks its inner lists in as claims")
    sig, why, _how = ci.claim_signal("a bare string", "b", 0)
    ok(sig is None and why == "not a claim object",
       "and the claim builder refuses a non-object outright, whatever "
       "reaches it")


def test_parent_hint_contract():
    print("parent hint contract")
    import jsonschema
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "contracts",
                           "extraction_schema.json")) as f:
        schema = json.load(f)

    def batch(sig_extra):
        s = {"signal_id": "s1", "type": "assignment",
             "title": "Count events per day", "confidence": 0.8,
             "evidence": [{"source_type": "transcript_utterance",
                           "resource_id": "u1", "span": None,
                           "content_hash": "h"}]}
        s.update(sig_extra)
        return {"batch_id": "b1", "extracted_at": "2026-08-31T10:00:00Z",
                "extractor": {"model": "m", "prompt_version": "v1"},
                "window": {"source_type": "meeting_transcript",
                           "source_ref": "r",
                           "time_start": "2026-08-31T10:00:00Z",
                           "time_end": "2026-08-31T10:30:00Z"},
                "signals": [s]}

    jsonschema.validate(batch({"parent_hint": "Stroke prediction work"}),
                        schema)
    ok(True, "a signal may name the work it belongs under")
    jsonschema.validate(batch({}), schema)
    ok(True, "and an extractor that cannot tell simply omits it -- every "
             "signal ever written stays valid, which is why this went in "
             "the emitted shape and not a column")
    try:
        jsonschema.validate(batch({"parent_hint": "x" * 121}), schema)
        ok(False, "an over-long hint is refused")
    except jsonschema.ValidationError:
        ok(True, "an over-long hint is refused: it is a task title, and "
                 "the column it will become one in is capped at 120")
    try:
        jsonschema.validate(batch({"project": "Stroke prediction"}), schema)
        ok(False, "a field nobody declared is refused")
    except jsonschema.ValidationError:
        ok(True, "and the field is parent_hint, not project -- the "
                 "contract still refuses anything it did not declare")


def test_quote_anchoring():
    print("quote anchoring")
    import tempfile
    import quote_anchor
    tmp = tempfile.mkdtemp()
    said = ("So the plan is to finalise the CGM cohort definition and get "
            "the patient counts over to Lorenzo by Thursday.")
    window = {"job": {"source_ref": "teams-2026-08-31-scrum"},
              "directory": {},
              "items": [
                  {"resource_id": "tx1-001", "author_ref": "aad-hari",
                   "text": said},
                  {"resource_id": "tx1-002", "author_ref": "aad-yesha",
                   "text": "I can pick up the observation extraction work "
                           "once the cohort is settled."}]}
    with open(os.path.join(tmp, "w.json"), "w") as f:
        json.dump(window, f)
    t = quote_anchor.Transcripts(tmp)
    ok(len(t) == 2, "utterances on disk are indexed")

    rec, how = t.find(said)
    ok(rec and rec["resource_id"] == "tx1-001" and how == "exact",
       "a verbatim quote finds its own utterance")
    ok(rec["content_hash"] == quote_anchor._sha(said),
       "and carries the same content hash the extraction worker writes, "
       "so a receipt verifies identically whichever path produced it")

    # What a model actually returns: the sentence, lightly tidied.
    rec, how = t.find("So the plan is to finalize the CGM cohort "
                      "definition and get the patient counts over to "
                      "Lorenzo by Thursday")
    ok(rec and rec["resource_id"] == "tx1-001" and how.startswith("fuzzy"),
       "a lightly reworded quote still matches -- models tidy grammar "
       "and drop fillers")

    # The failure that matters. A receipt pointing at the wrong sentence
    # is not a weaker citation, it is a lie with a hash on it.
    rec, how = t.find("We should migrate the whole platform to Kubernetes "
                      "next quarter and rewrite ingestion in Rust")
    ok(rec is None,
       "a sentence nobody said is REFUSED, not mapped to the nearest "
       "thing available")
    rec, how = t.find("yes agreed")
    ok(rec is None,
       "and a quote too short to identify anything is refused too")


def test_prompt_build():
    print("prompt construction")
    numbered = [dict(it, item_no=i + 1) for i, it in enumerate(SPEC["items"])]
    msgs = extraction_prompts.build_messages(
        "meeting_transcript", "2026-07-20", numbered)
    ok(msgs[0]["role"] == "system"
       and "completion_claim" in msgs[0]["content"]
       and "Do NOT emit" in msgs[0]["content"],
       "system prompt carries type spec and negative guidance")
    ok("[4] Alex" in msgs[1]["content"]
       and "2026-07-20" in msgs[1]["content"],
       "items numbered with authors; window end date anchored")

    # The empty-owner rule is right for a room and wrong for a mailbox, so
    # authored channels carry an exception -- and ONLY authored channels.
    # Every other source type must stay byte-identical, because that is what
    # lets the attribution numbers measured without it still stand.
    spoken = [extraction_prompts.build_messages(st, "2026-07-20",
                                                numbered)[0]["content"]
              for st in ("meeting_transcript", "teams_channel", "teams_chat")]
    authored = [extraction_prompts.build_messages(st, "2026-07-20",
                                                  numbered)[0]["content"]
                for st in extraction_prompts.AUTHORED_SOURCES]
    ok(all("AUTHORED MESSAGES" not in s for s in spoken),
       "spoken channels never see the authored-sender exception")
    ok(all("AUTHORED MESSAGES" in s for s in authored),
       "authored channels do")
    ok(len(set(spoken)) == 1 and len(set(authored)) == 1,
       "the prompt varies by channel class, not by individual source type")
    ok(authored[0].replace(extraction_prompts.AUTHORED_ATTRIBUTION, "")
       == spoken[0],
       "authored prompt is the spoken prompt plus that block and nothing "
       "else")


def test_golden():
    print("golden path")
    w, calls = make_worker(GOLDEN, wrap="prose")
    batch, report = w.run_job(SPEC["job"])
    ok(len(calls) == 1, "one LLM call per window")
    ok(report["kept"] == 5 and not report["dropped"],
       "all five signals kept despite prose-wrapped response")
    ok(batch["extractor"]["prompt_version"] == extraction_worker.PROMPT_VERSION
       and batch["window"]["source_ref"] == SPEC["job"]["source_ref"],
       "batch metadata populated")

    by_type = {s["type"]: s for s in batch["signals"]}
    a = by_type["assignment"]
    ok(a["owner_candidates"] == [{"person_ref": "aad-dev",
                                  "confidence": 0.9,
                                  "basis": "explicit_assignment"}],
       "display name resolved to person_ref with basis intact")
    ok(a["deadline"]["date"] == "2026-07-31"
       and a["deadline"]["raw_text"] == "end of next week",
       "deadline resolved date + raw phrase both carried")
    ok(len(a["evidence"]) == 2
       and a["evidence"][0]["resource_id"] == "utt-004"
       and len(a["evidence"][0]["content_hash"]) == 64,
       "citations became evidence refs with sha256 content hashes")
    b = by_type["blocker"]
    ok(b["blocker"]["description"].startswith("IAM role")
       and b["blocker"]["blocking_party_ref"] is None,
       "blocker carried; unknown party stays null")
    ok(by_type["proposal"]["owner_candidates"] == [],
       "unowned proposal keeps empty candidate list")
    # Author is who SAID it; owner is who it is FOR. The assignment is
    # spoken by Alex (utt-004) about work for Dev, and conflating the two
    # would make a third-party claim look like a self-report.
    ok(a["author_ref"] == "aad-alex"
       and a["owner_candidates"][0]["person_ref"] == "aad-dev",
       "author (speaker) recorded independently of owner")
    ok(all("author_ref" in s for s in batch["signals"]),
       "every signal carries an author field")
    ok(by_type["completion_claim"]["author_ref"] == "aad-dev",
       "author comes from the cited utterance, not the model")


def test_determinism():
    print("deterministic ids")
    w1, _ = make_worker(GOLDEN)
    w2, _ = make_worker(GOLDEN, wrap="fenced")
    b1, _ = w1.run_job(SPEC["job"])
    b2, _ = w2.run_job(SPEC["job"])
    ok(b1["batch_id"] == b2["batch_id"],
       "batch_id identical across re-runs")
    ok([s["signal_id"] for s in b1["signals"]] ==
       [s["signal_id"] for s in b2["signals"]],
       "signal_ids identical across re-runs (fenced vs bare response)")


def test_salvage():
    print("salvage of malformed signals")
    mixed = MALFORMED + [GOLDEN[0]]
    w, _ = make_worker(mixed)
    batch, report = w.run_job(SPEC["job"])
    ok(report["kept"] == 2 and len(batch["signals"]) == 2,
       "two salvageable signals survive malformed neighbors")
    reasons = {d["reason"].split(":")[0] for d in report["dropped"]}
    ok(reasons == {"bad_type", "missing_title", "no_valid_evidence",
                   "not_an_object"},
       "every drop reason recorded for prompt tuning")

    w2, _ = make_worker("this is not json at all")
    # llm returns non-list; simulate by making payload a plain string
    w2.llm = lambda messages: "I could not find any tasks, sorry!"
    batch2, report2 = w2.run_job(SPEC["job"])
    ok(batch2["signals"] == [] and
       report2["dropped"][0]["reason"] == "unparseable_response",
       "unparseable response yields valid empty batch, not a crash")


def test_broken_array_salvage():
    print("salvage of a broken array")
    # The shape a contradiction-dense window actually produced: thousands of
    # characters of well-formed signals, then the JSON breaks. Before this,
    # the break cost every signal before it.
    body = json.dumps(GOLDEN[0]) + "," + json.dumps(GOLDEN[1])
    truncated = "[" + body + ',{"type":"assignment","title":"cut off mid-'
    w, _ = make_worker([])
    w.llm = lambda messages: truncated
    batch, report = w.run_job(SPEC["job"])
    ok(report["kept"] == 2 and len(batch["signals"]) == 2,
       "signals before the break survive a truncated array")
    ok(report["salvaged"]["objects"] == 2
       and report["salvaged"]["broken"] == 1,
       "the report counts what was rescued AND what was lost")
    ok("parse_error" in report["salvaged"],
       "the original parse error is kept, so salvage is never silent")
    titles = [s["title"] for s in batch["signals"]]
    ok(not any("cut off mid-" in t for t in titles),
       "the truncated object is dropped, never closed or guessed")

    # A brace or quote inside a title must not shift the depth count.
    tricky = copy.deepcopy(GOLDEN[0])
    tricky["title"] = 'fix the {broken} "thing"'
    raw = "[" + json.dumps(tricky) + ',{"oops": ]'
    w2, _ = make_worker([])
    w2.llm = lambda messages: raw
    batch2, report2 = w2.run_job(SPEC["job"])
    ok(report2["kept"] == 1
       and batch2["signals"][0]["title"] == 'fix the {broken} "thing"',
       "braces and quotes inside strings do not confuse the scanner")

    w3, _ = make_worker(GOLDEN)
    _, report3 = w3.run_job(SPEC["job"])
    ok("salvaged" not in report3,
       "a well-formed response reports no salvage at all")


def test_meeting_recap():
    print("meeting recap")
    msgs = extraction_prompts.build_messages(
        "meeting_recap", "2026-08-04",
        [{"item_no": 1, "author_name": "Alex", "timestamp": "t",
          "text": "Prompt gallery direction"}])
    sysmsg = msgs[0]["content"]
    ok("MEETING RECAP" in sysmsg and "NOT utterances" in msgs[1]["content"],
       "a recap is framed as subjects reported, not things said")
    ok("AUTHORED MESSAGES" not in sysmsg,
       "and never as an authored message -- a recap has no sender, so the "
       "sender-owns-it rule would attribute everyone's work to nobody")

    # The model is told not to invent state. This is the half that does not
    # depend on it having listened.
    recap_job = {"source_type": "meeting_recap",
                 "source_ref": "recap-fixture",
                 "window_start": "2026-08-04T00:00:00Z",
                 "window_end": "2026-08-04T23:59:00Z"}
    payload = [
        {"type": "task_mention", "title": "Prompt gallery direction",
         "speaker": "Alex", "owner_candidates": [
             {"name": "Alex", "basis": "self_reference", "confidence": 0.95}],
         "status_signal": None, "deadline": None, "blocker": None,
         "confidence": 0.7, "evidence_items": [1]},
        {"type": "completion_claim", "title": "Rollout readiness",
         "speaker": "Alex", "owner_candidates": [],
         "status_signal": "completed", "deadline": None, "blocker": None,
         "confidence": 0.8, "evidence_items": [1]},
        {"type": "blocker", "title": "Governance review",
         "speaker": "Alex", "owner_candidates": [],
         "status_signal": "blocked", "deadline": None,
         "blocker": {"description": "waiting", "blocking_party": None},
         "confidence": 0.8, "evidence_items": [1]},
        {"type": "deadline_signal", "title": "Slide quality work",
         "speaker": "Alex", "owner_candidates": [],
         "status_signal": None,
         "deadline": {"raw_text": "next sprint", "precision": "vague",
                      "date": None},
         "blocker": None, "confidence": 0.8, "evidence_items": [1]},
    ]
    w, _ = make_worker(payload)
    w.fetcher = lambda job: [
        {"resource_id": "rc-1", "author_name": "Alex", "author_ref": "aad-alex",
         "timestamp": "2026-08-04T12:00:00Z", "text": "Prompt gallery direction"}]
    batch, report = w.run_job(recap_job)
    kinds = [s["type"] for s in batch["signals"]]
    ok(kinds == ["task_mention"],
       "only the sighting survives; a summary cannot witness a completion, "
       "a blocker or a deadline")
    reasons = set(d["reason"] for d in report["dropped"])
    ok(reasons == set(["recap_cannot_witness_state"]),
       "and each deletion is recorded with a reason, not silently dropped")
    ok(batch["signals"][0]["owner_candidates"][0]["basis"] == "inferred"
       and batch["signals"][0]["owner_candidates"][0]["confidence"] <= 0.6,
       "'discussed by' is an inference: a stronger basis is demoted, since "
       "nobody said anything for the model to have heard")
    ok(batch["signals"][0]["evidence"][0]["source_type"] == "meeting_insight",
       "evidence points at an insight, never at an utterance that does not "
       "exist")

    # The same payload through a transcript keeps everything: the guard is
    # about the source, not about the signal types.
    w2, _ = make_worker(payload)
    b2, _ = w2.run_job(SPEC["job"])
    ok(len(b2["signals"]) == 4,
       "a real transcript is unaffected -- state changes are only refused "
       "where they cannot have been witnessed")


def test_hallucination_guards():
    print("hallucination guards")
    bad = copy.deepcopy(GOLDEN[3])
    bad["deadline"]["date"] = "sometime in Q3"
    bad["owner_candidates"] = [
        {"name": "Zorp the Unknown", "basis": "self_reference",
         "confidence": 0.9},
        {"name": "dev", "basis": "made_up_basis", "confidence": 0.7}]
    w, _ = make_worker([bad])
    batch, report = w.run_job(SPEC["job"])
    s = batch["signals"][0]
    ok(s["deadline"]["date"] is None
       and s["deadline"]["raw_text"] == "end of next week",
       "non-ISO date nulled, raw phrase preserved")
    ok(s["owner_candidates"] == [{"person_ref": "aad-dev",
                                  "confidence": 0.7, "basis": "inferred"}],
       "unknown person dropped; case-insensitive match; bad basis demoted")


def test_empty_window():
    print("empty window")
    w, _ = make_worker([])
    w.fetcher = lambda job: []
    batch, report = w.run_job(SPEC["job"])
    ok(batch["signals"] == [] and report["items"] == 0,
       "empty window yields schema-valid empty batch, zero LLM calls")


def test_self_commitment_guard():
    """When the model attributes nobody and the speaker committed
    themselves, the speaker is the owner.

    Measured on the first live run: no owner candidates at all in 18 of 22
    rejected creates. Sentence shapes below follow those cases; the wording
    is invented.
    """
    print("self-commitment guard")
    import extraction_worker as w

    fires = ["And I'll work with Dana on the migration",
             "I'm going to have the file, so I'll update the tracker too",
             "let me pull those numbers together",
             "I've started on the loader rewrite"]
    for text in fires:
        got = w._self_commitment_owner({"text": text}, "aad-speaker")
        ok(got and got[0]["person_ref"] == "aad-speaker"
           and got[0]["basis"] == "self_reference",
           "a speaker committing themselves owns the work: {0!r}".format(
               text[:38]))

    elided = ["would also like to start on the prompt library",
              "Will start on the parser next week"]
    for text in elided:
        ok(w._self_commitment_owner({"text": text}, "aad-speaker"),
           "elided first person still commits: {0!r}".format(text[:38]))

    silent = ["can we prioritise the readme instead?",
              "Maybe part of onboarding could be a demo session",
              "I agree, I think let\'s have a basic lesson on this",
              "Did we have a meeting scheduled, Dana or Sam?",
              "Can you, one of you, reply to that thread?",
              "we should probably rewrite the loader"]
    for text in silent:
        ok(w._self_commitment_owner({"text": text}, "aad-speaker") == [],
           "names nobody, so the guard stays silent: {0!r}".format(
               text[:38]))

    ok(w._self_commitment_owner({"text": "I'll do it"}, None) == [],
       "no recorded speaker, no guess")
    ok(w._self_commitment_owner({"text": ""}, "aad-speaker") == [],
       "no text, no guess")


def test_guard_end_to_end():
    print("guard through the worker")
    spec = {
        "job": {"source_type": "meeting_transcript",
                "source_ref": "guard-window",
                "window_start": "2026-08-07T09:00:00Z",
                "window_end": "2026-08-07T10:00:00Z"},
        "directory": {"Dana": "aad-dana"},
        "items": [{"resource_id": "g-1", "author_name": "Dana",
                   "author_ref": "aad-dana",
                   "timestamp": "2026-08-07T09:05:00Z",
                   "text": "I'll take the loader rewrite this week"},
                  {"resource_id": "g-2", "author_name": "Dana",
                   "author_ref": "aad-dana",
                   "timestamp": "2026-08-07T09:06:00Z",
                   "text": "can we also look at the docs?"}],
    }
    # The model attributes nobody -- the shape the live run produced.
    response = [
        {"type": "assignment", "title": "Rewrite the loader",
         "owner_candidates": [], "status_signal": None, "deadline": None,
         "blocker": None, "confidence": 0.8, "evidence_items": [1]},
        {"type": "proposal", "title": "Look at the docs",
         "owner_candidates": [], "status_signal": None, "deadline": None,
         "blocker": None, "confidence": 0.7, "evidence_items": [2]},
    ]

    def llm(messages):
        return json.dumps(response)

    worker = extraction_worker.ExtractionWorker(
        fetcher=lambda job: spec["items"], llm=llm,
        directory=spec["directory"], model_name="fake:guard")
    batch, _ = worker.run_job(spec["job"])
    by_title = {s["title"]: s for s in batch["signals"]}
    committed = by_title["Rewrite the loader"]
    ok(committed["owner_candidates"] == [{"person_ref": "aad-dana",
                                          "basis": "self_reference",
                                          "confidence": 0.75}],
       "an unattributed self-commitment leaves the worker owned by its "
       "speaker")
    ok(by_title["Look at the docs"]["owner_candidates"] == [],
       "a request to the room leaves the worker unowned, as it should")
    ok(committed["author_ref"] == "aad-dana",
       "author and owner agree here, and are still recorded separately")


def test_pool_routing():
    """The rule that decides how many people get asked about one sentence.

    Measured on the first live run: unowned claims fanned out to every
    participant, so one sentence produced ten proposals and nine correct
    rejections. 15 of 22 wrong_owner rejections were that shape.
    """
    print("pool routing")
    import signal_store_pg as store
    participants = ["aad-a", "aad-b", "aad-c", "aad-d"]

    owned = {"owner_candidates": [{"person_ref": "aad-b"},
                                  {"person_ref": "aad-c"}],
             "author_ref": "aad-a"}
    targets, how = store.pool_targets(owned, participants)
    ok(targets == ["aad-b", "aad-c"] and how == "owners",
       "an attributed claim goes to its owners, not to its speaker")

    unowned = {"owner_candidates": [], "author_ref": "aad-a"}
    targets, how = store.pool_targets(unowned, participants)
    ok(targets == ["aad-a"] and how == "author",
       "an unowned claim goes to the person who said it -- one card, not "
       "one per participant")

    anonymous = {"owner_candidates": [], "author_ref": None}
    targets, how = store.pool_targets(anonymous, participants)
    ok(targets == [] and how == "unrouted",
       "with no owner and no speaker it reaches nobody's card, rather than "
       "everybody's: the claim stays in the ledger and is counted")

    targets, how = store.pool_targets(anonymous, participants,
                                      broadcast_unowned=True)
    ok(targets == participants and how == "broadcast",
       "the old fan-out is still available, behind an explicit flag")

    empty_refs = {"owner_candidates": [{"person_ref": None}, {}],
                  "author_ref": "aad-a"}
    targets, how = store.pool_targets(empty_refs, participants)
    ok(targets == ["aad-a"] and how == "author",
       "candidates with no resolved ref do not count as attribution")


def test_pg_store():
    print("postgres store (live)")
    dsn = os.environ.get(
        "DB_DSN",
        "host=/tmp/pgsock port=5433 dbname=taskintel user=postgres")
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
    except Exception as e:
        print("  skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return
    conn.close()
    import signal_store_pg
    w, _ = make_worker(GOLDEN)
    batch, _ = w.run_job(SPEC["job"])
    fallback = sorted(set(SPEC["directory"].values()))
    r1 = signal_store_pg.write_batch(dsn, batch, fallback)
    ok(r1["signals_inserted"] in (0, 5),
       "first write inserts (or batch already present from a prior run)")
    r2 = signal_store_pg.write_batch(dsn, batch, fallback)
    ok(r2["signals_inserted"] == 0 and r2["pool_events"] == 0
       and r2["already_present"] == 5,
       "re-write is a no-op: idempotent on content-hash ids")
    import psycopg2
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        # The invariant extraction owns: dev's signals from this batch
        # were POOLED. Whether they are currently unconsumed is the
        # business of whatever reconciled after us -- a prior reconciler
        # suite run legitimately takes them (found the hard way on a
        # fresh Windows port, where strict suite ordering exposed the
        # stateful assumption).
        cur.execute("""
            SELECT count(DISTINCT e.signal_id)
            FROM signal_pool_events e
            JOIN signals s USING (signal_id)
            WHERE e.user_ref = 'aad-dev' AND e.event = 'pooled'
              AND s.batch_id = %s""",
            (batch["batch_id"],))
        n = cur.fetchone()[0]
        if n < 2:
            cur.execute("SELECT count(*) FROM signals WHERE batch_id=%s",
                        (batch["batch_id"],))
            print("  DIAG signals rows for batch:", cur.fetchone()[0])
            cur.execute("""
                SELECT signal_id, user_ref, event FROM signal_pool_events
                WHERE signal_id IN (SELECT signal_id FROM signals
                                    WHERE batch_id=%s)
                ORDER BY event_id""", (batch["batch_id"],))
            rows = cur.fetchall()
            print("  DIAG pool events ({0}):".format(len(rows)))
            for r_ in rows:
                print("    ", r_[0][:12], r_[1], r_[2])
            cur.execute("""
                SELECT user_ref, signal_type FROM v_unconsumed_signals
                WHERE batch_id=%s""", (batch["batch_id"],))
            print("  DIAG view rows:", cur.fetchall())
            cur.execute("SHOW server_version")
            print("  DIAG server_version:", cur.fetchone()[0])
    conn.close()
    ok(n >= 2, "dev's assignment + completion signals were pooled (later consumption is legitimate)")


if __name__ == "__main__":
    test_transcript_encodings()
    test_copilot_paste_shapes()
    test_quote_anchoring()
    test_parent_hint_contract()
    test_prompt_build()
    test_golden()
    test_determinism()
    test_salvage()
    test_broken_array_salvage()
    test_meeting_recap()
    test_hallucination_guards()
    test_empty_window()
    test_self_commitment_guard()
    test_guard_end_to_end()
    test_pool_routing()
    test_pg_store()
    print("\nall {0} extraction checks passed".format(CHECKS[0]))
