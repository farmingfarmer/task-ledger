"""Synthetic messy-comms generator for extraction stress testing.

Simulates what Graph would actually deliver: a workgroup's day across four
channel styles, rendered with channel-appropriate mess, plus a golden-label
sidecar for scoring. No Mnemo, no real data -- a seeded scenario engine.

  meeting_transcript   fillers, interruptions, run-ons that pack two signals
                       into one utterance, crosstalk
  teams_channel        lowercase, typos, abbreviations (eod/tmrw/lgtm),
                       thread interleaving, drive-by +1s
  teams_chat (1:1)     terse, pronoun-heavy, context assumed
  email                greetings, signatures, quoted history below the
                       actual content

Mess dimensions beyond formatting, each labeled in the golden file:
  - negation traps: "we're NOT doing X", "no longer blocked on Y" -- the
    trap is extracting the affirmative
  - third-party completion claims (should extract but downstream gates
    demand corroboration)
  - vague deadlines ("Friday-ish", "before the audit") vs resolvable ones
  - name variants (Alexander/Alex/AM) exercising the directory
  - pure distractor chatter (lunch, praise, scheduling) -- extracting
    anything from it is a false positive
  - duplicate mentions of one task across channels (dedup burden lands on
    the reconciler; extraction should report each sighting)

Usage:
  python3 synthesize.py --seed 7 --days 2 --out corpus/
Emits corpus/window-*.json (inbox job format, cadence-compatible) and
corpus/golden.json. Deterministic per seed. Python 3.8, stdlib only.
"""

import argparse
import datetime
import json
import os
import random

CAST = [
    {"ref": "aad-alex",   "name": "Alexander Marunycz",
     "variants": ["Alex", "Alexander", "AM"]},
    {"ref": "aad-priya",  "name": "Priya Raman",
     "variants": ["Priya", "P"]},
    {"ref": "aad-dev",    "name": "Dev Patel",
     "variants": ["Dev"]},
    {"ref": "aad-mallory","name": "Mallory Chen",
     "variants": ["Mallory", "Mal"]},
    {"ref": "aad-vic",    "name": "Victor Ruiz",
     "variants": ["Vic", "Victor"]},
]

TASKS = [
    ("Readmission model feature drift monitor", "drift monitor"),
    ("Intake OCR preprocessing fix", "OCR fix"),
    ("Fargate IAM role for prod deploy", "IAM role"),
    ("Retraining pipeline alerting", "alerting"),
    ("Feature store documentation refresh", "feature store docs"),
    ("Nightly ETL memory ceiling", "ETL memory"),
    ("Model card for compliance review", "model card"),
    ("Sepsis dashboard latency", "sepsis dashboard"),
]

DISTRACTORS = [
    "anyone want thai for lunch? going at noon",
    "great demo yesterday {name}, seriously nice work",
    "can we move the 1:1 to thursday? dentist",
    "the espresso machine on 4 is fixed, you're welcome everyone",
    "did anyone else's vpn drop around 10?",
    "reminder: benefits enrollment closes friday",
    "hah yeah that thread on the data eng forum was wild",
]

VAGUE_DEADLINES = ["sometime soon", "Friday-ish", "before the audit",
                   "when you get a chance", "in the next sprint or two"]
FIRM_DEADLINES = [("end of next week", "week"), ("this Friday", "day"),
                  ("by the 15th", "day"), ("end of month", "month")]

FILLERS = ["uh, ", "yeah so ", "okay -- ", "right, um, ", ""]
CHAT_MANGLE = [("tomorrow", "tmrw"), ("end of day", "eod"),
               ("by the way", "btw"), ("I think", "i think"),
               ("going to", "gonna"), ("the", "teh")]


def pick(rng, seq):
    return seq[rng.randrange(len(seq))]


def person(rng, exclude=None):
    while True:
        p = pick(rng, CAST)
        if p["ref"] != exclude:
            return p


def name_of(rng, p, channel):
    if channel == "email":
        return p["name"]
    return pick(rng, p["variants"] + [p["name"]])


def mangle_chat(rng, text):
    text = text.lower()
    for a, b in CHAT_MANGLE:
        if rng.random() < 0.4:
            text = text.replace(a, b)
    if rng.random() < 0.25:   # dropped apostrophes
        text = text.replace("'", "")
    return text


def wrap_transcript(rng, text):
    return pick(rng, FILLERS) + text


def wrap_email(rng, author, text, thread_subject):
    sig = "\n\n{0}\n{1}".format(author["name"],
                                "Data Science | Keck Medicine of USC")
    quoted = ("\n\n> On an earlier note re: {0}, see thread below."
              .format(thread_subject)) if rng.random() < 0.5 else ""
    return "Hi team,\n\n{0}{1}{2}".format(text, quoted, sig)


# --------------------------------------------------------------- event pool

def build_events(rng, day_tasks):
    """Returns a list of (kind, payload) task-relevant events plus traps.
    Density: 1-2 events per task. Guarantees per day: at least one negation
    trap, one third-party completion, one vague deadline, and two
    cross-channel duplicate sightings -- the hard cases are never left to
    the dice."""
    events = []
    for title, alias in day_tasks:
        for _ in range(1 + (rng.random() < 0.6)):
            events.append(_roll_event(rng, title, alias))

    kinds_present = {k for k, _ in events}
    forced = [("negation_trap", lambda t, a: {
                  "speaker": person(rng), "title": t, "alias": a,
                  "flavor": pick(rng, ["not_doing", "unblocked",
                                       "deadline_dropped"])}),
              ("completion_third", lambda t, a: (lambda o: {
                  "speaker": person(rng, exclude=o["ref"]), "about": o,
                  "title": t, "alias": a})(person(rng))),
              ("vague_deadline", lambda t, a: {
                  "speaker": person(rng), "title": t, "alias": a,
                  "phrase": pick(rng, VAGUE_DEADLINES)})]
    for kind, builder in forced:
        if kind not in kinds_present:
            t, a = pick(rng, day_tasks)
            events.append((kind, builder(t, a)))

    # cross-channel duplicate sightings: echo two signal-bearing events
    bearers = [(k, e) for k, e in events
               if k in ("status", "completion_self", "assignment",
                        "blocker")]
    for k, e in rng.sample(bearers, min(2, len(bearers))):
        events.append(("mention", {"speaker": person(rng),
                                   "title": e["title"],
                                   "alias": e["alias"]}))

    rng.shuffle(events)
    return events


def _roll_event(rng, title, alias):
    owner = person(rng)
    kind = rng.random()
    if kind < 0.16:
        assignee = person(rng, exclude=owner["ref"])
        dl = pick(rng, FIRM_DEADLINES) if rng.random() < 0.6 else None
        return ("assignment", {
            "speaker": owner, "assignee": assignee, "title": title,
            "alias": alias, "deadline": dl})
    if kind < 0.34:
        return ("status", {
            "speaker": owner, "title": title, "alias": alias,
            "to": pick(rng, ["in_progress", "in_progress", "blocked"])})
    if kind < 0.46:
        blocker_party = person(rng, exclude=owner["ref"])
        return ("blocker", {
            "speaker": owner, "title": title, "alias": alias,
            "party": blocker_party})
    if kind < 0.56:
        return ("completion_self", {
            "speaker": owner, "title": title, "alias": alias})
    if kind < 0.64:
        reporter = person(rng, exclude=owner["ref"])
        return ("completion_third", {
            "speaker": reporter, "about": owner, "title": title,
            "alias": alias})
    if kind < 0.72:
        return ("proposal", {
            "speaker": owner, "title": title, "alias": alias})
    if kind < 0.80:
        return ("negation_trap", {
            "speaker": owner, "title": title, "alias": alias,
            "flavor": pick(rng, ["not_doing", "unblocked",
                                 "deadline_dropped"])})
    if kind < 0.88:
        return ("vague_deadline", {
            "speaker": owner, "title": title, "alias": alias,
            "phrase": pick(rng, VAGUE_DEADLINES)})
    return ("mention", {"speaker": owner, "title": title, "alias": alias})


# ------------------------------------------------------------- render event

def render(rng, kind, ev, channel):
    """-> (text, golden_signal_or_None, trap_dict_or_None)"""
    sp = ev["speaker"]
    t = ev.get("title", "")
    alias = ev.get("alias", t)
    use = alias if rng.random() < 0.55 else t

    if kind == "assignment":
        a = ev["assignee"]
        aname = name_of(rng, a, channel)
        dl = ev["deadline"]
        dtxt = " -- can you have it {0}?".format(dl[0]) if dl else "."
        text = "{0}, can you take the {1}{2}".format(aname, use, dtxt)
        if rng.random() < 0.4:
            text += " " + pick(rng, ["It keeps slipping.",
                                     "Priya flagged it in review.",
                                     "Should be mostly wiring."])
        golden = {"type": "assignment", "title_core": use,
                  "owner_ref": a["ref"], "basis": "explicit_assignment",
                  "deadline_precision": dl[1] if dl else None}
        return text, golden, None

    if kind == "status":
        if ev["to"] == "blocked":
            text = "quick update on the {0}: stuck at the moment, more later"\
                .format(use)
        else:
            text = "making decent progress on the {0}, midway through"\
                .format(use)
        return text, {"type": "status_update", "title_core": use,
                      "owner_ref": sp["ref"], "basis": "self_reference",
                      "status": ev["to"]}, None

    if kind == "blocker":
        pname = name_of(rng, ev["party"], channel)
        text = "the {0} is blocked -- waiting on {1}'s team for the review"\
            .format(use, pname)
        return text, {"type": "blocker", "title_core": use,
                      "owner_ref": sp["ref"]}, None

    if kind == "completion_self":
        text = pick(rng, [
            "{0} is done, shipped it this morning".format(use),
            "finally closed out the {0}, went out in last night's release"
            .format(use)])
        return text, {"type": "completion_claim", "title_core": use,
                      "owner_ref": sp["ref"], "basis": "self_reference"}, None

    if kind == "completion_third":
        oname = name_of(rng, ev["about"], channel)
        text = "i think {0} finished the {1} yesterday? board still shows "\
               "it open".format(oname, use)
        return text, {"type": "completion_claim", "title_core": use,
                      "owner_ref": ev["about"]["ref"],
                      "basis": "inferred", "third_party": True}, None

    if kind == "proposal":
        text = "we should really {0} at some point, it keeps biting us"\
            .format(pick(rng, ["rebuild the " + use,
                               "get the " + use + " sorted",
                               "put time into the " + use]))
        return text, {"type": "proposal", "title_core": use}, None

    if kind == "negation_trap":
        flavor = ev["flavor"]
        if flavor == "not_doing":
            text = "decision from planning: we are NOT doing the {0} this "\
                   "quarter, park it".format(use)
            trap = {"trap": "extracted_parked_work", "title_core": use,
                    "acceptable": ["cancellation"]}
        elif flavor == "unblocked":
            text = "good news, the {0} is no longer blocked, cloud team "\
                   "came through".format(use)
            trap = {"trap": "extracted_blocker_from_unblock",
                    "title_core": use, "acceptable": ["blocker_resolved",
                                                      "status_update"]}
        else:
            text = "fyi the deadline on the {0} is dropped, no date for now"\
                .format(use)
            trap = {"trap": "extracted_deadline_from_removal",
                    "title_core": use, "acceptable": []}
        return text, None, trap

    if kind == "vague_deadline":
        text = "would be good to land the {0} {1}".format(use, ev["phrase"])
        return text, {"type_any": ["deadline_signal", "proposal",
                                   "status_update", "task_mention"],
                      "title_core": use,
                      "deadline_precision": "vague"}, None

    if kind == "mention":
        text = "related: this overlaps with the {0} a bit".format(use)
        return text, {"type_any": ["task_mention", "status_update",
                                   "proposal"],
                      "title_core": use, "mention_only": True}, None

    raise ValueError(kind)


# ------------------------------------------------------------------ windows

CHANNELS = [
    ("meeting_transcript", "standup", datetime.time(9, 0), 15),
    ("teams_channel", "ml-eng channel", datetime.time(10, 30), 90),
    ("teams_chat", "dm", datetime.time(13, 0), 20),
    ("email", "thread", datetime.time(15, 0), 60),
]


def synthesize_day(rng, date, day_index):
    day_tasks = rng.sample(TASKS, 6)
    events = build_events(rng, day_tasks)
    windows = []
    per_window = max(1, len(events) // len(CHANNELS))
    chunks = [events[i:i + per_window]
              for i in range(0, len(events), per_window)][:len(CHANNELS)]

    for (source_type, label, start, minutes), chunk in zip(CHANNELS, chunks):
        t0 = datetime.datetime.combine(date, start,
                                       tzinfo=datetime.timezone.utc)
        t1 = t0 + datetime.timedelta(minutes=minutes)
        items, golden_signals, traps = [], [], []
        n = 0

        def emit(author, text):
            nonlocal n
            n += 1
            items.append({
                "resource_id": "{0}-d{1}-{2:03d}".format(
                    source_type[:5], day_index, n),
                "author_name": author["name"],
                "author_ref": author["ref"],
                "timestamp": (t0 + (t1 - t0) * (n / (len(chunk) * 2 + 4)))
                    .isoformat().replace("+00:00", "Z"),
                "text": text,
            })

        if source_type == "meeting_transcript":
            emit(pick(rng, CAST), "morning everyone, quick round, "
                                  "keep it tight")
        for kind, ev in chunk:
            text, golden, trap = render(rng, kind, ev, source_type)
            if source_type == "meeting_transcript":
                text = wrap_transcript(rng, text)
                if rng.random() < 0.25:   # interruption crosstalk
                    emit(person(rng, exclude=ev["speaker"]["ref"]),
                         pick(rng, ["sorry, one sec --",
                                    "wait, which one is that?",
                                    "we can take that offline"]))
            elif source_type in ("teams_channel", "teams_chat"):
                text = mangle_chat(rng, text)
            elif source_type == "email":
                text = wrap_email(rng, ev["speaker"], text, "yesterday's sync")
            emit(ev["speaker"], text)
            if golden:
                golden["evidence_hint"] = items[-1]["resource_id"]
                golden_signals.append(golden)
            if trap:
                trap["evidence_hint"] = items[-1]["resource_id"]
                traps.append(trap)
            if source_type == "teams_channel" and rng.random() < 0.5:
                emit(person(rng), mangle_chat(rng, pick(rng, DISTRACTORS)
                     .format(name=name_of(rng, ev["speaker"],
                                          source_type))))
        # trailing distractors
        for _ in range(rng.randrange(1, 3)):
            emit(person(rng), pick(rng, DISTRACTORS).format(
                name=pick(rng, CAST)["variants"][0]))

        job = {"source_type": ("meeting_transcript"
                               if source_type == "meeting_transcript"
                               else "teams_channel"
                               if source_type == "teams_channel"
                               else "teams_chat"
                               if source_type == "teams_chat"
                               else "email_thread"),
               "source_ref": "{0}-{1}-d{2}".format(source_type, label
                                                   .replace(" ", "-"),
                                                   day_index),
               "window_start": t0.isoformat().replace("+00:00", "Z"),
               "window_end": t1.isoformat().replace("+00:00", "Z")}
        windows.append({
            "job": job,
            "directory": {v: p["ref"] for p in CAST
                          for v in ([p["name"]] + p["variants"])},
            "items": items,
            "_golden": {"signals": golden_signals, "traps": traps,
                        "distractor_items": None},
        })
    return windows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--out", default="corpus")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)
    golden_all = {}
    count = 0
    base = datetime.date(2026, 7, 20)
    for d in range(args.days):
        date = base + datetime.timedelta(days=d)
        for w in synthesize_day(rng, date, d):
            count += 1
            golden = w.pop("_golden")
            fname = "window-{0:02d}-{1}.json".format(count,
                                                     w["job"]["source_ref"])
            with open(os.path.join(args.out, fname), "w") as f:
                json.dump(w, f, indent=1)
            golden_all[fname] = golden
    with open(os.path.join(args.out, "golden.json"), "w") as f:
        json.dump(golden_all, f, indent=1)
    print("wrote {0} windows + golden.json to {1}/ (seed {2})".format(
        count, args.out, args.seed))


if __name__ == "__main__":
    main()
