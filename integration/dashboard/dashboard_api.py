"""Dashboard API, viewer-aware.

Every request carries a viewer identity; every task-reading query joins
through visible_tasks(viewer) -- the single authorization function defined in
access_control.sql. The API never reads tasks_current for another user
directly, so a policy change in that one SQL function changes every endpoint
at once and there is no second code path to forget.

Identity: X-Viewer header here for local demo. In production this comes from
the validated AAD token on the request; nothing else changes.

Access levels and what they unlock:
  progress    board state + history skeleton. Rationales, decider identity,
              confidence, blocker text, and provenance are REDACTED -- these
              are derived from internal communications the stakeholder was
              not part of.
  full_trace  everything down to evidence pointers.
Owners are always full_trace on their own tasks.

Endpoints:
  GET /api/me                        viewer, boards visible, grants received
  GET /api/team                      Q5 rollup over VISIBLE tasks only
  GET /api/board/<user_ref>          Q1 filtered; includes project + level
  GET /api/task/<id>/history         Q2; redacted at progress level
  GET /api/event/<id>/provenance     Q3; 403 unless full_trace on the task
  GET /api/blocked-on/<user_ref>     Q4; owner or full_trace board grant only
  GET /api/grants                    grants the viewer has given (audit view)
  GET /api/pending                   Q6; SELF ONLY
  GET /api/held                      pooled, unconsumed claims; SELF ONLY
  GET /api/calibration               Q7 + Q8 on own decisions; SELF ONLY
  GET /api/viewers                   demo identity picker; off by default
  GET /api/ask?q=...                 answered from confirmed state, cited
  GET /api/meetings                  follow-through per conversation
  GET /api/meeting/<source_ref>      every claim one conversation produced
  GET /api/metrics                   the measures behind the charts
  GET /api/review                    the nudge popup: unruled claims about
                                     you, plus your work that has gone
                                     quiet; SELF ONLY
  POST /api/decide                   record a decision on one proposal
  POST /api/nudge                    type where a quiet task got to

The two POSTs are the only writes in this file and they are refused unless
the active profile is synthetic: the real ledger's session stays read-only,
because a browser path into colleague data is exactly what that choice
prevents. On a synthetic ledger they show the invariant working end to end
-- an accept goes through cli/review.py's apply_accept() into
apply_task_event(), and a nudge writes actor_type='manual_edit' with a NULL
decision_id, so "a person typed this" stays distinguishable from "a model
proposed it and a person agreed" forever.

SELF ONLY is a stricter rule than the grant ladder: pending packets, held
claims, and reject reasons are unreviewed model output about a person and
what they judged wrong about it. A full_trace grant covers the confirmed
board, never this. These endpoints filter on the viewer's own ref and
consult no grant at all.

Sessions are READ ONLY: grants are written through grant_access()/
revoke_access() in SQL (wired to the bot's /share command or an admin flow),
never through this API. Python 3.8 compatible.
"""

import datetime
import decimal
import hashlib
import json
import os
import re
import sys

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

DSN = os.environ.get(
    "DB_DSN", "host=/tmp/pgsock port=5433 dbname=taskintel user=postgres")

# Two ledgers, one dashboard. Demoing this system means showing real
# extraction on real conversations AND a synthetic set wide enough to cover
# interactions the real week happened not to contain -- and switching
# between them must be one click, not an edited environment and a restart.
#
# The switch is a NAME, never a DSN. A client that could hand this endpoint
# a connection string could point the dashboard at any database reachable
# from the host, so the request selects from a server-built whitelist and
# anything unrecognised falls back to the default rather than erroring into
# some other profile. The synthetic profile exists only when TL_SYNTH_DSN is
# set; with it unset this file behaves exactly as it did before.
SYNTH_DSN = os.environ.get("TL_SYNTH_DSN", "").strip()
COPILOT_DSN = os.environ.get("TL_COPILOT_DSN", "").strip()

# `writable` is separate from `synthetic` because they answer different
# questions. Synthetic says whether the content is invented; writable says
# whether a browser may record decisions against it. Only invented content
# is writable here -- every ledger holding real colleague material keeps the
# read-only session that is the reason a browser cannot reach it.
PROFILES = {
    # system_of_record: this board is the answer to "what am I working
    # on". The other two are instruments -- one invented, one built to be
    # compared against this. The distinction decides which rules protect
    # a board from bad evidence and which rules would only prevent a
    # measurement. See _unacceptable().
    "real": {"dsn": DSN, "synthetic": False, "writable": False,
             "system_of_record": True,
             "label": os.environ.get("TL_REAL_LABEL", "Real ledger"),
             "blurb": "Extracted from your own conversations.",
             "banner": None},
}
if SYNTH_DSN:
    PROFILES["synthetic"] = {
        "dsn": SYNTH_DSN, "synthetic": True, "writable": True,
        "system_of_record": False,
        "label": os.environ.get("TL_SYNTH_LABEL", "Synthetic demo"),
        "blurb": "Generated meetings: invented work, planted owner labels.",
        "banner": {
            "tag": "Synthetic data",
            "text": "Generated meetings spoken by the real directory about "
                    "invented work. Owner labels were planted before "
                    "extraction ran, so every attribution on this screen is "
                    "checkable against a known answer. Nothing here is a "
                    "real task -- and every confirmation was seeded in one "
                    "sitting, so the said-to-tracked timings measure that "
                    "sitting, not the pipeline."}}
# The comparison ledger's conventional name is taskintel_copilot -- it
# is what `tl copilot --apply` creates, on the same server as the real
# one. Requiring a hand-set TL_COPILOT_DSN to see it meant the profile
# silently vanished every time the dashboard started from a fresh
# window, and the operator read the missing button as a broken build --
# three times. So when the variable is unset, probe for the database by
# its conventional name and use it if it answers. The variable still
# overrides; TL_DISABLE_COPILOT_PROBE=1 turns the probe off (the test
# suite does, so its profile assumptions stay its own).
if (not COPILOT_DSN and DSN
        and os.environ.get("TL_DISABLE_COPILOT_PROBE") != "1"):
    _parts = dict(kv.split("=", 1) for kv in DSN.split() if "=" in kv)
    _parts["dbname"] = "taskintel_copilot"
    _cand = " ".join("{0}={1}".format(k, v) for k, v in _parts.items())
    try:
        psycopg2.connect(_cand, connect_timeout=2).close()
        COPILOT_DSN = _cand
        print("copilot ledger: found taskintel_copilot on the same "
              "server -- offering it (set TL_COPILOT_DSN to override, "
              "TL_DISABLE_COPILOT_PROBE=1 to stop looking)")
    except Exception:                                   # noqa: BLE001
        pass

if COPILOT_DSN:
    PROFILES["copilot"] = {
        "dsn": COPILOT_DSN, "synthetic": False, "writable": False,
        "system_of_record": False,
        "label": os.environ.get("TL_COPILOT_LABEL", "Copilot claims"),
        "blurb": "Claims Copilot exported, traceable only where the quote "
                 "was found in a transcript we hold.",
        "banner": {
            "tag": "Assistant claims, anchored where possible",
            "text": "Copilot extracted these itself. Where it quoted a "
                    "sentence we could find in the transcript, the claim "
                    "cites that utterance by id and hash and carries the "
                    "speaker who actually said it -- those cards are "
                    "answerable. Where the quote matched nothing, the "
                    "claim keeps its cap, its owner stays 'inferred', and "
                    "the card is read-only: accepting something nobody can "
                    "check is not a decision anyone should be able to "
                    "record. The board still moves only when a person "
                    "agrees."}}

# HANDS-ON MODE. The real ledger is read-only from a browser on purpose:
# a decision is a person's word, and a web page that anyone can point at
# should not be able to speak for them. That reasoning holds for a
# deployed system and stops holding for the case this was built for --
# the team whose own meetings produced the ledger, sitting down together
# to answer their own cards and see what the thing actually does.
#
# So it is a switch, not a default: off unless TL_HANDS_ON=1 is set by
# whoever starts the server, announced in the UI while it is on, and
# never silent.
#
# The copilot profile used to stay read-only in every mode, because
# nothing on it could be traced to a sentence. Quote anchoring changed
# what is true there, but only for some cards: a claim whose quote was
# found in a transcript we hold cites that utterance by id and hash, and
# a claim whose quote matched nothing still cites nobody. So the gate
# moved from the profile to the card -- see _untraceable(). A mixed
# ledger must not be wholly writable just because part of it earned it.
HANDS_ON = os.environ.get("TL_HANDS_ON", "").strip() in ("1", "true", "yes")
if HANDS_ON:
    PROFILES["real"]["writable"] = True
    if COPILOT_DSN:
        PROFILES["copilot"]["writable"] = True
    PROFILES["real"]["banner"] = {
        "tag": "Hands-on session",
        "text": "Your answers are real: accepting a card puts the task on "
                "your board and rejecting it is recorded with your name "
                "and reason. This is the ledger of your own meetings, so "
                "nothing here is invented -- but nothing moves until you "
                "say so, and every answer can be corrected afterwards."}

DEFAULT_PROFILE = os.environ.get("TL_DEFAULT_PROFILE", "real")
if DEFAULT_PROFILE not in PROFILES:
    DEFAULT_PROFILE = "real"

REDACTED = "[shared at progress level - detail hidden]"


# BLIND MODE. Two ledgers built from the same meetings by different
# extractors is a comparison, and a comparison whose arms are labelled
# "Real ledger" and "Copilot claims" is not one -- the names carry an
# answer, and so did the banners, which explained at length what was
# wrong with one arm before anyone had read a card.
#
# What is blinded is the LABELLING, never the data. If one arm's cards
# cannot be traced to a sentence, the reviewer should meet that and rate
# it: that is the thing being measured, not a leak. Removing it would be
# rigging the comparison in the other direction.
#
# Which arm is which is randomised PER PERSON and stable for them, so
# order effects do not all land the same way and nobody can compare
# notes at lunch and decode it. The mapping is never sent to the browser;
# it is written into the survey file, which is for the person collecting
# the answers.
BLIND = os.environ.get("TL_BLIND", "").strip() in ("1", "true", "yes")
BLIND_SALT = os.environ.get("TL_BLIND_SALT", "task-ledger-blind-v1")
BLIND_ARMS = ("real", "copilot")
ARM_NAMES = ("arm-a", "arm-b")


def blind_map(v):
    """viewer -> {'arm-a': profile, 'arm-b': profile}, or {} if not blind."""
    arms = [n for n in BLIND_ARMS if n in PROFILES]
    if not BLIND or len(arms) < 2:
        return {}
    digest = hashlib.sha256(
        (BLIND_SALT + "\x1f" + (v or "")).encode("utf-8")).hexdigest()
    if int(digest[:8], 16) % 2:
        arms = list(reversed(arms))
    return dict(zip(ARM_NAMES, arms))


def profile_name():
    """The requested profile, or the default. Never the client's string."""
    try:
        asked = (request.args.get("profile") or "").strip()
    except RuntimeError:                      # outside a request context
        return DEFAULT_PROFILE
    mapping = blind_map(viewer())
    if mapping:
        # Under blinding the real names are not addressable, including by
        # someone typing one into the URL bar. Anything unrecognised
        # resolves to that person's OWN arm A -- falling back to
        # DEFAULT_PROFILE would quietly mean "real" and hand the answer
        # to whoever guessed.
        return mapping.get(asked, mapping[ARM_NAMES[0]])
    return asked if asked in PROFILES else DEFAULT_PROFILE


def profile_dsn():
    return PROFILES[profile_name()]["dsn"]


def q(sql, params=()):
    conn = psycopg2.connect(profile_dsn())
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_clean(dict(r)) for r in rows]
    finally:
        conn.close()


def _clean(row):
    for k, v in row.items():
        if isinstance(v, (datetime.datetime, datetime.date)):
            row[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            row[k] = float(v)
    return row


def viewer():
    v = request.headers.get("X-Viewer", "").strip()
    return v or None


def task_level(v, task_id):
    rows = q("SELECT task_access(%s, %s) AS lvl", (v, task_id))
    return rows[0]["lvl"] if rows else None


def _need_viewer():
    return jsonify({"error": "no viewer identity on request"}), 401


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)),
                               "dashboard.html")


@app.route("/assets/<path:filename>")
def assets(filename):
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    return send_from_directory(base, filename)


def _build_id():
    """Which code is actually running, from git if it is there.

    "I pulled and nothing changed" has three causes -- the pull did not
    land, the server was not restarted, or the browser served a cached
    page -- and from the outside they look identical. A build stamp on
    the page and in the startup banner tells the three apart in one
    glance instead of an exchange of guesses.
    """
    try:
        import subprocess
        root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        out = subprocess.check_output(
            ["git", "-C", root, "log", "-1", "--format=%h %cd",
             "--date=format:%Y-%m-%d %H:%M"],
            stderr=subprocess.DEVNULL).decode("utf-8").strip()
        if out:
            return out
    except Exception:                                   # noqa: BLE001
        pass
    # A kit has no .git -- it is a git-archive export -- so the header
    # read "build unknown" on exactly the machines where knowing the
    # build matters most: a teammate's, mid bug report. The kit builder
    # stamps BUILD_ID at packaging time; read it back here.
    try:
        root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        with open(os.path.join(root, "BUILD_ID")) as f:
            return f.read().strip() or "unknown"
    except Exception:                                   # noqa: BLE001
        return "unknown"


BUILD_ID = _build_id()


@app.after_request
def _no_stale_page(resp):
    """Never let a browser answer with yesterday's dashboard.

    This is one HTML file carrying its own JavaScript, so a cached copy
    is a cached APPLICATION -- every fix shipped since the page was
    first loaded is simply absent, and the person looking at it has no
    way to tell. The API answers are per-request and per-viewer and
    should not be stored either.
    """
    ctype = (resp.headers.get("Content-Type") or "")
    if ctype.startswith("text/html") or ctype.startswith("application/json"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    resp.headers["X-TL-Build"] = BUILD_ID
    return resp


@app.route("/api/version")
def version():
    return jsonify({"build": BUILD_ID, "blind": bool(blind_map(viewer())),
                    "hands_on": HANDS_ON,
                    "ledgers": [n for n in ("real", "synthetic", "copilot")
                                if n in PROFILES],
                    "kit": ({"for": KIT.get("for"),
                             "for_name": KIT.get("for_name"),
                             "return_to": KIT.get("return_to"),
                             "answers_dir": SURVEY_DIR}
                            if KIT and KIT.get("for") else None)})


@app.route("/api/profiles")
def profiles():
    """Which ledgers this dashboard can show, and which one is active.

    Deliberately no DSNs: a connection string names a host and a database
    user, and nothing in the UI needs either. The client gets names it may
    ask for and labels it may print.
    """
    active = profile_name()
    mapping = blind_map(viewer())
    if mapping:
        # Two arms, identically described. The synthetic ledger is left
        # out entirely: it is a demo, not an arm, and a third option that
        # obviously differs from the other two is a hint about what the
        # other two are.
        out = []
        for arm in ARM_NAMES:
            out.append({
                "name": arm,
                "label": "Ledger " + arm[-1].upper(),
                "blurb": "One of two ledgers built from the same meetings. "
                         "Which is which is not shown, on purpose.",
                "synthetic": False,
                "writable": PROFILES[mapping[arm]].get("writable", False),
                "banner": {
                    "tag": "Blind comparison",
                    "text": "Both ledgers were built from the same "
                            "meetings by different means, and which you "
                            "are looking at is deliberately not shown -- "
                            "the point is to judge the cards, not the "
                            "method. Answer them as you would your own. "
                            "Nothing moves without you."},
                "active": mapping[arm] == active,
            })
        return jsonify({"profiles": out, "active": _active_arm(mapping,
                                                               active),
                        "default": ARM_NAMES[0], "switchable": True,
                        "blind": True})
    out = []
    for name in ("real", "synthetic", "copilot"):
        if name in PROFILES:
            p = PROFILES[name]
            out.append({"name": name, "label": p["label"],
                        "blurb": p["blurb"], "synthetic": p["synthetic"],
                        "writable": p.get("writable", False),
                        "banner": p.get("banner"),
                        "active": name == active})
    return jsonify({"profiles": out, "active": active,
                    "default": DEFAULT_PROFILE,
                    "switchable": len(out) > 1, "blind": False})


def _active_arm(mapping, active):
    for arm, real in mapping.items():
        if real == active:
            return arm
    return ARM_NAMES[0]


# ---------------------------------------------------------------- survey
#
# The point of handing this to five colleagues is not that they use it.
# It is to find out what it gets WRONG, from people who were in the
# meetings and can tell. That answer has a short half-life: it is worth
# most in the ten seconds after someone has just ruled on a card, and it
# is gone by the time anyone opens their email to write it up. So the
# form arrives there, and writes a file they can send on without having
# to describe anything twice.
# PERSONAL KIT. A kit built with `tl kit --for <person>` carries a
# kit-config.json naming who it is for and where the answers go back to.
# The point is to remove every decision a recipient could get wrong:
# they are greeted by name, their identity is fixed (picking the wrong
# name in a dropdown records DECISIONS under the wrong person -- the one
# unrecoverable mistake this UI offered), and the final screen says
# exactly what to send and to whom.
KIT = None
_kit_path = os.environ.get("TL_KIT_CONFIG")
if _kit_path and os.path.exists(_kit_path):
    try:
        with open(_kit_path) as _f:
            KIT = json.load(_f)
    except Exception as _exc:                           # noqa: BLE001
        print("kit-config.json unreadable ({0!r}) -- running as a "
              "generic kit".format(_exc))

SURVEY_DIR = os.path.expanduser(
    os.environ.get("TL_SURVEY_DIR", "~/task-ledger-surveys"))

SURVEY_QUESTIONS = [
    {"id": "recognised", "kind": "scale",
     "text": "Did the cards describe work you actually recognise?",
     "low": "not at all", "high": "yes, all of it"},
    {"id": "attribution", "kind": "scale",
     "text": "When a card said whose work it was, was it right?",
     "low": "usually wrong", "high": "usually right"},
    {"id": "junk", "kind": "choice",
     "text": "How many were things that should never have been a card at all?",
     "options": ["none", "one or two", "several", "most of them"]},
    {"id": "missed", "kind": "text",
     "text": "What did it MISS that mattered? Something you know was agreed "
             "in those meetings and never appeared."},
    {"id": "worst", "kind": "text",
     "text": "What was the single worst card you saw, and what was wrong "
             "with it?"},
    {"id": "keep", "kind": "choice",
     "text": "Would you rather have this than nothing?",
     "options": ["yes", "not yet", "no"]},
    {"id": "anything", "kind": "text",
     "text": "Anything else. Blunt is useful."},
]

COMPARE_QUESTIONS = [
    {"id": "which_better", "kind": "choice",
     "text": "You have now reviewed both ledgers. Which asked better "
             "questions?",
     "options": ["Ledger A", "Ledger B", "no real difference"]},
    {"id": "which_why", "kind": "text",
     "text": "What made the difference? (If there wasn't one, say that.)"},
]


def _survey_path(v, arm):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (v or "x"))
    return os.path.join(SURVEY_DIR,
                        "survey-{0}-{1}-{2}".format(safe, arm, stamp))


def _submitted_arms(v):
    """Arms this person has already answered for, from the files on disk.

    Deliberately not stored in either ledger. A survey answer is about the
    ledger; putting it inside one would mean the thing being judged also
    holds the judgement, and it would land in whichever arm happened to be
    open at the time.
    """
    out = set()
    try:
        for name in sorted(os.listdir(SURVEY_DIR)):
            if not name.endswith(".json") or not name.startswith("survey-"):
                continue
            try:
                with open(os.path.join(SURVEY_DIR, name)) as f:
                    doc = json.load(f)
            except Exception:                           # noqa: BLE001
                continue
            if doc.get("viewer") == v and doc.get("arm"):
                out.add(doc["arm"])
    except OSError:
        pass
    return out


def _decision_counts(dsn, v):
    """What this person actually DID, beside what they say about it.

    Self-report and behaviour disagree often enough that collecting one
    without the other wastes the session, and this half needs nobody to
    type anything.
    """
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute("""
                SELECT d.decision, d.reject_reason, count(*) AS n
                FROM decisions d
                WHERE d.decided_by = %s
                GROUP BY d.decision, d.reject_reason""", (v,))
            rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    out = {"accept": 0, "accept_edited": 0, "reject": 0, "defer": 0,
           "reject_reasons": {}}
    for r in rows:
        out[r["decision"]] = out.get(r["decision"], 0) + int(r["n"])
        if r["decision"] == "reject" and r["reject_reason"]:
            out["reject_reasons"][r["reject_reason"]] = (
                out["reject_reasons"].get(r["reject_reason"], 0) + int(r["n"]))
    return out


@app.route("/api/survey")
def survey_form():
    """The questions to ask, and whether the comparison pair is due yet."""
    v = viewer()
    if not v:
        return _need_viewer()
    mapping = blind_map(v)
    arm = _active_arm(mapping, profile_name()) if mapping else profile_name()
    done = _submitted_arms(v)
    questions = list(SURVEY_QUESTIONS)
    # The comparison question is only answerable by someone who has seen
    # both, and asking it earlier would collect a guess.
    if mapping and done and arm not in done:
        questions = questions + COMPARE_QUESTIONS
    return jsonify({"arm": arm, "questions": questions,
                    "already_done": sorted(done),
                    "submitted": arm in done,
                    "remaining": sorted(a for a in (mapping or {})
                                        if a != arm and a not in done)})


@app.route("/api/survey", methods=["POST"])
def survey_submit():
    v = viewer()
    if not v:
        return _need_viewer()
    body = request.get_json(silent=True) or {}
    answers = body.get("answers")
    if not isinstance(answers, dict) or not answers:
        return jsonify({"error": "no answers"}), 400
    mapping = blind_map(v)
    arm = _active_arm(mapping, profile_name()) if mapping else profile_name()

    doc = {
        "viewer": v,
        "arm": arm,
        # What the arm actually WAS. The participant stays blind; the file
        # is for whoever reads the answers, and an answer nobody can
        # attribute to a ledger is worth nothing.
        "ledger": (mapping.get(arm) if mapping else profile_name()),
        "blind": bool(mapping),
        "submitted_at": datetime.datetime.now().replace(
            microsecond=0).isoformat(),
        "answers": {k: a for k, a in answers.items() if isinstance(k, str)},
        "questions": {q["id"]: q["text"]
                      for q in SURVEY_QUESTIONS + COMPARE_QUESTIONS},
    }
    try:
        doc["decisions"] = _decision_counts(profile_dsn(), v)
    except Exception as exc:                            # noqa: BLE001
        doc["decisions"] = {"error": repr(exc)}   # never lose a typed answer

    try:
        os.makedirs(SURVEY_DIR, exist_ok=True)
        stem = _survey_path(v, arm)
        with open(stem + ".json", "w") as f:
            json.dump(doc, f, indent=1)
        # A readable twin, because "send me the file" goes better when the
        # person sending it can see what they are sending.
        lines = ["Task ledger -- review feedback",
                 "person: " + v,
                 "ledger: " + str(doc["arm"]),
                 "when:   " + doc["submitted_at"], ""]
        for q in SURVEY_QUESTIONS + COMPARE_QUESTIONS:
            if q["id"] in doc["answers"]:
                lines.append(q["text"])
                lines.append("    " + str(doc["answers"][q["id"]]))
                lines.append("")
        counts = doc["decisions"]
        if isinstance(counts, dict) and "error" not in counts:
            lines.append("what they did: {0} accepted, {1} rejected, "
                         "{2} deferred".format(
                             counts.get("accept", 0)
                             + counts.get("accept_edited", 0),
                             counts.get("reject", 0), counts.get("defer", 0)))
            if counts.get("reject_reasons"):
                lines.append("rejections: " + ", ".join(
                    "{0} x{1}".format(k, n)
                    for k, n in sorted(counts["reject_reasons"].items())))
        with open(stem + ".txt", "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        return jsonify({"error": "could not write the answers",
                        "detail": "{0}. Set TL_SURVEY_DIR to a folder you "
                                  "can write to.".format(exc)}), 500

    return jsonify({"ok": True, "file": stem + ".txt", "folder": SURVEY_DIR,
                    "remaining": sorted(a for a in (mapping or {})
                                        if a != arm
                                        and a not in _submitted_arms(v))})


@app.route("/api/me")
def me():
    v = viewer()
    if not v:
        return _need_viewer()
    boards = q("""
        SELECT t.user_ref, count(*) AS visible_tasks,
               bool_or(vt.access_level = 'full_trace') AS any_full_trace
        FROM visible_tasks(%s) vt JOIN tasks_current t USING (task_id)
        WHERE t.merged_into IS NULL
        GROUP BY t.user_ref ORDER BY t.user_ref""", (v,))
    received = q("""
        SELECT subject_type, subject_id, level, granted_by, occurred_at
        FROM v_current_grants WHERE grantee_ref = %s
        ORDER BY occurred_at""", (v,))
    return jsonify({"viewer": v, "boards": boards,
                    "grants_received": received})


@app.route("/api/viewers")
def viewers():
    """Demo-only identity picker, OFF unless TL_VIEWER_PICKER=1.

    Every other task-reading endpoint joins through visible_tasks(); this
    one cannot, because it answers "who could I sign in as" before there
    is a viewer to scope by. Enumerating everyone who owns a task leaks
    the existence of people and their work -- the same no-count-leakage
    rule the team rollup obeys. In production the viewer is the AAD
    identity on the request and this endpoint does not exist at all.
    """
    if os.environ.get("TL_VIEWER_PICKER") != "1":
        return jsonify({"error": "viewer picker disabled; set "
                                 "TL_VIEWER_PICKER=1 for local demos"}), 404
    # Board owners AND anyone with something waiting on them. A ledger
    # freshly imported from an assistant's claims has no board at all --
    # nothing is on one until a person agrees -- so an owners-only list is
    # empty and there is no identity to review as, which makes the review
    # queue unreachable exactly when it is the only thing to see.
    rows = q("""
        SELECT DISTINCT r FROM (
          SELECT owner_ref AS r FROM tasks_current
           WHERE owner_ref IS NOT NULL AND owner_ref <> ''
          UNION
          SELECT user_ref AS r FROM review_packets
          UNION
          SELECT user_ref AS r FROM v_unconsumed_signals
        ) x WHERE r IS NOT NULL AND r <> '' ORDER BY 1""")
    return jsonify({"viewers": [x["r"] for x in rows]})


@app.route("/api/pending")
def pending():
    """Q6 for the viewer's own packets: what is waiting on them.

    Owner-only, and not weakened by a full_trace board grant: a pending
    packet is unreviewed model output about someone, which is a strictly
    more sensitive thing than the confirmed board a grant covers.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    rows = q("""
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT rp.packet_id, l.state, rp.generated_at, rp.expires_at,
               count(p.proposal_id)                        AS proposals,
               count(p.proposal_id) - count(d.decision_id) AS undecided,
               count(*) FILTER (WHERE p.conflict_type IS NOT NULL
                                  AND d.decision_id IS NULL) AS conflicts
        FROM review_packets rp
        JOIN latest l USING (packet_id)
        JOIN proposals p USING (packet_id)
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')
        GROUP BY rp.packet_id, l.state, rp.generated_at, rp.expires_at
        HAVING count(p.proposal_id) - count(d.decision_id) > 0
        ORDER BY rp.generated_at""", (v,))
    return jsonify({"packets": rows,
                    "undecided": sum(r["undecided"] for r in rows),
                    "conflicts": sum(r["conflicts"] for r in rows)})


@app.route("/api/held")
def held():
    """Claims pooled for the viewer and not yet consumed by a packet.

    The gates that hold a signal (corroboration, closure strictness) leave
    it in the pool rather than guessing, and until now that state was
    invisible outside psql -- so work the system knows about but cannot
    yet propose looked identical to work it never heard. Owner-only: these
    are unreviewed claims, the same sensitivity as a pending packet.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT u.signal_id, u.signal_type, u.title, u.confidence,
               u.deadline_raw, u.status_signal,
               (SELECT count(*) FROM evidence_refs e
                 WHERE e.signal_id = u.signal_id)          AS sources,
               (SELECT min(pe.occurred_at) FROM signal_pool_events pe
                 WHERE pe.signal_id = u.signal_id
                   AND pe.user_ref = u.user_ref)           AS pooled_at
        FROM v_unconsumed_signals u
        WHERE u.user_ref = %s
        ORDER BY pooled_at DESC NULLS LAST, u.signal_id""", (v,)))


@app.route("/api/calibration")
def calibration():
    """Q7 + Q8 over the viewer's OWN decisions: the feedback loop the
    matching policy calibrates on (MATCHING_POLICY section 5).

    Reject reasons are a record of what a person judged wrong about claims
    made in their communications, so this is owner-only and never widened
    by a grant. Drift is the current week against the trailing four-week
    mean; the histogram routes fixes -- duplicate to thresholds,
    bad_date_resolution to extraction, wrong_task_match to auto-link.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    drift = q("""
        WITH weekly AS (
          SELECT date_trunc('week', d.decided_at) AS wk, p.op,
                 count(*) FILTER (WHERE d.decision IN ('accept',
                                                       'accept_edited'))::numeric
                   / count(*) AS accept_rate,
                 count(*) AS n
          FROM decisions d
          JOIN proposals p USING (proposal_id)
          JOIN review_packets rp ON rp.packet_id = p.packet_id
          WHERE d.decision <> 'defer' AND rp.user_ref = %s
          GROUP BY 1, 2)
        SELECT wk, op, round(accept_rate, 3) AS accept_rate, n,
               round(avg(accept_rate) OVER (PARTITION BY op ORDER BY wk
                     ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING), 3)
                 AS trailing_4wk,
               COALESCE((accept_rate - avg(accept_rate) OVER (
                     PARTITION BY op ORDER BY wk
                     ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING))
                 < -0.15, false) AS drift_alarm
        FROM weekly ORDER BY op, wk""", (v,))
    rejects = q("""
        SELECT p.op, d.reject_reason, count(*) AS n
        FROM decisions d
        JOIN proposals p USING (proposal_id)
        JOIN review_packets rp ON rp.packet_id = p.packet_id
        WHERE d.decision = 'reject' AND rp.user_ref = %s
        GROUP BY p.op, d.reject_reason
        ORDER BY n DESC, p.op""", (v,))
    decided = q("""
        SELECT count(*) AS n FROM decisions d
        JOIN proposals p USING (proposal_id)
        JOIN review_packets rp ON rp.packet_id = p.packet_id
        WHERE rp.user_ref = %s""", (v,))
    return jsonify({"drift": drift, "rejects": rejects,
                    "decisions_total": decided[0]["n"] if decided else 0})


@app.route("/api/team")
def team():
    v = viewer()
    if not v:
        return _need_viewer()
    # Rollup over visible tasks only: a viewer's counts never reveal the
    # existence of work they were not granted.
    return jsonify(q("""
        SELECT t.user_ref,
               count(*) FILTER (WHERE t.status = 'in_progress') AS in_progress,
               count(*) FILTER (WHERE t.status = 'blocked')     AS blocked,
               count(*) FILTER (WHERE t.status = 'proposed')    AS proposed,
               count(*) FILTER (WHERE t.status = 'completed'
                                AND t.updated_at > now() - interval '14 days')
                                                                AS completed_recent,
               count(*) FILTER (WHERE t.deadline < CURRENT_DATE
                                AND t.status NOT IN ('completed','cancelled'))
                                                                AS overdue
        FROM visible_tasks(%s) vt
        JOIN tasks_current t USING (task_id)
        WHERE t.merged_into IS NULL
        GROUP BY t.user_ref ORDER BY t.user_ref""", (v,)))


@app.route("/api/board/<user_ref>")
def board(user_ref):
    v = viewer()
    if not v:
        return _need_viewer()
    rows = q("""
        SELECT t.task_id, t.title, t.status, t.owner_ref, t.deadline,
               t.project_id, pr.name AS project_name,
               vt.access_level,
               (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
                AND t.status NOT IN ('completed','cancelled')) AS overdue,
               COALESCE(b.open_blockers, 0)  AS open_blockers,
               COALESCE(c.subtasks, 0)       AS subtasks,
               COALESCE(c.subtasks_done, 0)  AS subtasks_done,
               t.updated_at,
               floor(EXTRACT(epoch FROM now() - t.updated_at) / 86400)::int
                 AS quiet_days,
               dl.deadline_raw,
               COALESCE(src.n, 0) AS sources
        FROM visible_tasks(%s) vt
        JOIN tasks_current t USING (task_id)
        LEFT JOIN projects pr USING (project_id)
        LEFT JOIN (SELECT task_id, count(*) AS open_blockers FROM task_blockers
                   WHERE resolved_event_id IS NULL GROUP BY task_id) b
               USING (task_id)
        -- Progress, counted rather than stored: every subtask completion
        -- is already a human-decided event, so the fraction inherits that
        -- provenance and cannot drift from the log. Computed inline rather
        -- than read from v_task_progress so the board still renders on a
        -- ledger that predates that view.
        LEFT JOIN (SELECT parent_task_id AS task_id, count(*) AS subtasks,
                          count(*) FILTER (WHERE status = 'completed')
                            AS subtasks_done
                   FROM tasks_current
                   WHERE parent_task_id IS NOT NULL AND merged_into IS NULL
                   GROUP BY parent_task_id) c USING (task_id)
        -- The phrase someone actually said ("end of next sprint"), carried
        -- from the deadline event. Relative-date resolution is the top
        -- failure mode, and the resolved date alone hides it.
        LEFT JOIN LATERAL (
            SELECT e.payload->>'raw_text' AS deadline_raw
            FROM task_events e
            WHERE e.task_id = t.task_id AND e.event_type = 'deadline_changed'
              AND e.payload->>'raw_text' IS NOT NULL
            ORDER BY e.board_version DESC LIMIT 1) dl ON true
        -- How many distinct claims stand behind the confirmed state: one
        -- mention and five mentions are not the same kind of fact.
        LEFT JOIN LATERAL (
            SELECT count(DISTINCT ps.signal_id) AS n
            FROM task_events e
            JOIN decisions d         ON d.decision_id = e.decision_id
            JOIN proposal_signals ps ON ps.proposal_id = d.proposal_id
            WHERE e.task_id = t.task_id) src ON true
        WHERE t.user_ref = %s AND t.merged_into IS NULL
        ORDER BY overdue DESC, t.status, t.deadline NULLS LAST""",
        (v, user_ref))
    _add_last_noted(rows, v)
    _add_collaborators(rows, user_ref)
    out = []
    for r in rows:
        # A raw deadline phrase is a quote, and a source count is a fact
        # about someone's communications -- both belong to the provenance
        # rail, not the progress skeleton.
        if r["access_level"] != "full_trace":
            r["deadline_raw"] = None
            r["sources"] = None
            # Who said it is provenance; THAT it changed is progress.
            if r.get("last_noted"):
                r["last_noted"]["on_whose_word"] = None
        if r["access_level"] == "status_only":
            # Visible because it blocks the viewer's own work. They get the
            # title and whether it is moving -- not the deadline, project,
            # owner, blocker counts, or how quiet it has gone.
            r = {"task_id": r["task_id"], "title": r["title"],
                 "status": r["status"], "access_level": "status_only",
                 "blocking_you": True}
        out.append(r)
    return jsonify(out)


def _add_last_noted(rows, viewer_ref):
    """When each task was last moved, by what, and on whose word.

    The single most useful line on a task card and the one thing only this
    system can say: not "updated 3 days ago" but "reported blocked, on
    Priya's word, in Tuesday's standup". Speaker comes from the signals
    behind the decision, so it is absent for anything confirmed before the
    author_ref migration -- the card degrades to the event alone rather
    than inventing a name.
    """
    ids = [r["task_id"] for r in rows]
    if not ids:
        return
    events = q("""
        SELECT DISTINCT ON (e.task_id)
               e.task_id, e.event_id, e.event_type, e.actor_type,
               e.occurred_at, e.payload,
               d.decided_by,
               (SELECT string_agg(DISTINCT s.author_ref, ', ')
                  FROM proposal_signals ps
                  JOIN signals s ON s.signal_id = ps.signal_id
                 WHERE ps.proposal_id = d.proposal_id
                   AND s.author_ref IS NOT NULL) AS on_whose_word
        FROM task_events e
        LEFT JOIN decisions d ON d.decision_id = e.decision_id
        WHERE e.task_id = ANY(%s)
        ORDER BY e.task_id, e.board_version DESC""", (ids,))
    by_task = {e["task_id"]: e for e in events}
    for r in rows:
        e = by_task.get(r["task_id"])
        r["last_noted"] = None if not e else {
            "event_type": e["event_type"], "occurred_at": e["occurred_at"],
            "actor_type": e["actor_type"], "event_id": e["event_id"],
            "payload": e["payload"], "decided_by": e["decided_by"],
            "on_whose_word": e["on_whose_word"]}


def _add_collaborators(rows, board_ref):
    """Who else is attached to each task, and how.

    Three relations, in the order a person cares about them: someone whose
    work this is blocking, someone this is waiting on, and someone sharing
    the project. All of it is already visible on the board -- this only
    says it on the card instead of three panels away.
    """
    ids = [r["task_id"] for r in rows]
    if not ids:
        return
    linked = {}

    def add(task_id, ref, relation):
        if not ref or ref == board_ref:
            return
        bucket = linked.setdefault(task_id, [])
        if not any(x["ref"] == ref and x["relation"] == relation
                   for x in bucket):
            bucket.append({"ref": ref, "relation": relation})

    for b in q("""
            SELECT b.blocking_task_id AS on_task, w.user_ref AS waiter
            FROM task_blockers b
            JOIN tasks_current w ON w.task_id = b.task_id
            WHERE b.blocking_task_id = ANY(%s)
              AND b.resolved_event_id IS NULL""", (ids,)):
        # Keyed to the task being waited ON, not the one doing the waiting.
        add(b["on_task"], b["waiter"], "waiting on this")
    for b in q("""
            SELECT b.task_id, blk.user_ref AS blocker_owner,
                   b.blocking_party
            FROM task_blockers b
            LEFT JOIN tasks_current blk ON blk.task_id = b.blocking_task_id
            WHERE b.task_id = ANY(%s) AND b.resolved_event_id IS NULL""",
            (ids,)):
        add(b["task_id"], b["blocker_owner"] or b["blocking_party"],
            "you are blocked on them")
    for p in q("""
            SELECT mine.task_id, peer.user_ref
            FROM tasks_current mine
            JOIN tasks_current peer ON peer.project_id = mine.project_id
                                   AND peer.user_ref <> mine.user_ref
            WHERE mine.task_id = ANY(%s) AND mine.project_id IS NOT NULL
              AND peer.merged_into IS NULL""", (ids,)):
        add(p["task_id"], p["user_ref"], "shares the project")

    for r in rows:
        r["with"] = linked.get(r["task_id"], [])


@app.route("/api/task/<task_id>/history")
def history(task_id):
    v = viewer()
    if not v:
        return _need_viewer()
    level = task_level(v, task_id)
    if level is None:
        return jsonify({"error": "task not shared with you"}), 403
    if level == "status_only":
        # A blocker edge earns the current status, not the story of how the
        # task got there -- that is the whole point of the narrow level.
        return jsonify({"error": "this task is visible to you only because "
                                 "it blocks your work: status only, no "
                                 "history"}), 403
    rows = q("""
        SELECT e.event_id, e.board_version, e.occurred_at, e.event_type,
               e.actor_type, e.payload,
               d.decision, d.decided_by, d.decided_at,
               p.proposal_id, p.rationale, p.confidence,
               (d.decision_id IS NOT NULL) AS has_provenance,
               -- heard_in arrived in the payload only after the pilot's
               -- first reviewer proved "typed by hand" was a lie on
               -- accepted claims. Events written BEFORE that carry no
               -- link and never will (append-only) -- but the aliases
               -- their accepts taught DO carry source_signal_id, so the
               -- meeting is recoverable for most of them. Payload wins
               -- when present; the alias chain rescues the history.
               COALESCE(e.payload->>'heard_in',
                 CASE WHEN e.event_type = 'created'
                       AND e.actor_type = 'manual_edit' THEN
                   (SELECT b.source_ref
                    FROM task_aliases a
                    JOIN signals s ON s.signal_id = a.source_signal_id
                    JOIN extraction_batches b USING (batch_id)
                    WHERE a.task_id = e.task_id
                    ORDER BY a.added_at LIMIT 1)
                 END) AS heard_in
        FROM task_events e
        LEFT JOIN decisions d ON d.decision_id = e.decision_id
        LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
        WHERE e.task_id = %s
        ORDER BY e.board_version""", (task_id,))
    if level == "progress":
        rows = [_redact_event(r) for r in rows]
    return jsonify({"access_level": level, "events": rows})


def _redact_event(r):
    # The history skeleton (what changed, when, by which class of actor)
    # is progress-level information. Everything derived from internal
    # communications -- rationale, who decided, model confidence, blocker
    # wording -- is not.
    r["rationale"] = REDACTED if r.get("rationale") else None
    r["decided_by"] = None
    r["confidence"] = None
    r["proposal_id"] = None
    r["has_provenance"] = False
    payload = dict(r.get("payload") or {})
    if r["event_type"] == "blocker_added":
        payload["description"] = REDACTED
        payload.pop("blocking_party", None)
        payload.pop("blocking_task_id", None)
    if r["event_type"] == "alias_added":
        payload["alias"] = REDACTED
        payload.pop("source_signal_id", None)
    r["payload"] = payload
    return r


@app.route("/api/event/<int:event_id>/provenance")
def provenance(event_id):
    v = viewer()
    if not v:
        return _need_viewer()
    owner_rows = q("SELECT task_id FROM task_events WHERE event_id = %s",
                   (event_id,))
    if not owner_rows:
        return jsonify({"error": "no such event"}), 404
    if task_level(v, owner_rows[0]["task_id"]) != "full_trace":
        return jsonify({"error":
            "provenance requires full-trace access to this task"}), 403
    rows = q("""
        SELECT p.proposal_id, p.op, p.rationale, p.confidence,
               s.signal_id, s.signal_type, s.title AS signal_title,
               s.confidence AS signal_confidence, s.author_ref,
               ev.ordinal, ev.source_type, ev.resource_id,
               ev.span_start, ev.span_end, ev.content_hash
        FROM task_events e
        JOIN decisions d         ON d.decision_id = e.decision_id
        JOIN proposals p         ON p.proposal_id = d.proposal_id
        JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
        JOIN signals s           ON s.signal_id = ps.signal_id
        JOIN evidence_refs ev    ON ev.signal_id = s.signal_id
        WHERE e.event_id = %s
        ORDER BY s.signal_id, ev.ordinal""", (event_id,))
    for r in rows:
        r.update(_evidence_state(r))
    return jsonify(rows)


def _evidence_state(row):
    """The integrity state of one evidence pointer.

    CONTRACTS.md promises the UI flags a hash mismatch rather than quietly
    showing different text -- but that check needs a fetcher to re-read the
    message, and locally there is none. Saying so explicitly is the honest
    version: an unchecked hash must not look like a checked one. When the
    Graph fetcher lands it fills in verified / changed / unavailable here,
    and the UI already renders all four.
    """
    if not row.get("content_hash"):
        return {"integrity": "unhashed",
                "integrity_note": "no content hash was recorded"}
    if not os.environ.get("TL_EVIDENCE_FETCH"):
        return {"integrity": "unverified",
                "integrity_note": "hash not checked - no message fetcher "
                                  "configured"}
    return {"integrity": "unverified",
            "integrity_note": "fetcher configured but verification is not "
                              "wired yet"}


@app.route("/api/blocked-on/<user_ref>")
def blocked_on(user_ref):
    v = viewer()
    if not v:
        return _need_viewer()
    if v != user_ref:
        grants = q("""
            SELECT 1 FROM v_current_grants
            WHERE grantee_ref = %s AND subject_type = 'user'
              AND subject_id = %s AND level = 'full_trace'""", (v, user_ref))
        if not grants:
            return jsonify({"error":
                "the dependency view is owner-only unless the board is "
                "shared at full trace"}), 403
    return jsonify(q("""
        SELECT waiter.user_ref AS blocked_user,
               waiter.task_id  AS blocked_task,
               waiter.title    AS blocked_title,
               b.description   AS blocker_desc,
               mine.task_id    AS my_blocking_task,
               mine.title      AS my_blocking_title,
               mine.status     AS my_task_status
        FROM task_blockers b
        JOIN tasks_current waiter ON waiter.task_id = b.task_id
        LEFT JOIN tasks_current mine ON mine.task_id = b.blocking_task_id
        WHERE b.resolved_event_id IS NULL
          AND (mine.owner_ref = %s OR b.blocking_party = %s)
        ORDER BY waiter.user_ref""", (user_ref, user_ref)))


# Where a claim ended up, most conclusive first. A signal can be linked to
# several proposals across packets -- proposed, deferred, re-reconciled
# after a supersede -- so the raw join fans out and one claim would read as
# four. The follow-through report is per CLAIM, so the rows collapse to the
# outcome that actually describes it: landing on the board outranks the
# rejections that preceded it, and a claim back in the pool is "waiting"
# however it got there.
OUTCOME_RANK = ["done", "cancelled", "on the board", "waiting",
                "proposed, undecided", "deferred", "rejected", "no proposal"]


def _claim_rows(v, source_ref=None):
    """Per-claim follow-through for the viewer's own conversations."""
    params = [v, v]
    where = ""
    if source_ref is not None:
        where = "AND regexp_replace(b.source_ref, '-p[0-9]+$', '') = %s"
        params.append(source_ref)
    rows = q("""
        SELECT s.signal_id, s.signal_type, s.title, s.confidence,
               s.author_ref, s.deadline_raw,
               regexp_replace(b.source_ref, '-p[0-9]+$', '') AS source_ref,
               b.source_type, b.batch_id, b.window_start,
               d.decision, d.reject_reason, p.op,
               t.task_id, t.title AS task_title, t.status AS task_status,
               (u.signal_id IS NOT NULL) AS still_pooled
        FROM signals s
        JOIN extraction_batches b ON b.batch_id = s.batch_id
        JOIN signal_pool_events pe ON pe.signal_id = s.signal_id
                                  AND pe.user_ref = %s
        LEFT JOIN proposal_signals ps ON ps.signal_id = s.signal_id
        LEFT JOIN proposals p         ON p.proposal_id = ps.proposal_id
        LEFT JOIN decisions d         ON d.proposal_id = ps.proposal_id
        LEFT JOIN task_events e       ON e.decision_id = d.decision_id
        LEFT JOIN tasks_current t     ON t.task_id = e.task_id
        LEFT JOIN v_unconsumed_signals u
               ON u.signal_id = s.signal_id AND u.user_ref = %s
        WHERE true {0}""".format(where), tuple(params))
    best = {}
    for r in rows:
        r["outcome"] = _outcome(r)
        prev = best.get(r["signal_id"])
        if prev is None or (OUTCOME_RANK.index(r["outcome"])
                            < OUTCOME_RANK.index(prev["outcome"])):
            best[r["signal_id"]] = r
    return sorted(best.values(),
                  key=lambda r: (r["source_ref"], r["signal_id"]))


@app.route("/api/ask")
def ask_endpoint():
    """Answer a question from the ledger, scoped to the asker.

    The retrieval set is what visible_tasks(viewer) returns, so an answer
    cannot mention work the asker could not already see, and the sentences
    are assembled from rows rather than written by a model -- see ask.py.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    question = (request.args.get("q") or "").strip()
    if not question:
        return jsonify({"error": "ask something with ?q="}), 400
    if len(question) > 300:
        question = question[:300]
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("../../ask", "../../reconciler"):
        p = os.path.normpath(os.path.join(here, rel))
        if p not in sys.path:
            sys.path.insert(0, p)
    import ask as ask_mod
    import ask_io_pg
    # profile_dsn(), not DSN: ask builds its own connection rather than
    # going through q(), so the module-level default would have quietly
    # answered from the real ledger while the rest of the page showed the
    # synthetic one.
    return jsonify(ask_mod.answer(question,
                                  ask_io_pg.Retriever(profile_dsn(), v)))


# ---------------------------------------------------------------- review
# The nudge popup: what was heard that you have not ruled on, and what you
# said you would do that has gone quiet. In production this is the Teams
# Adaptive Card (review_loop/review_card.py); this is the same content over
# HTTP so it can be shown in a browser.
# How long silence has to last before the popup asks. A policy number, so
# it lives in the environment rather than in a literal: a freshly seeded
# demo ledger has every board event stamped today, and at 7 days nothing is
# ever quiet, so that half of the popup renders empty and the feature does
# not exist as far as an audience is concerned.
QUIET_DAYS = int(os.environ.get("TL_QUIET_DAYS", "7"))


def _writable():
    """Whether this profile accepts decisions from a browser.

    Read-only is the default and the reason a browser cannot speak for
    somebody: a decision carries a person's name, and a page anyone can
    point at should not be able to record one. The synthetic profile is
    writable because nothing on it is real. The REAL profile becomes
    writable only under TL_HANDS_ON=1 -- deliberately set by whoever
    starts the server, for the team whose own meetings made the ledger --
    and says so on screen the whole time it is.
    """
    return bool(PROFILES.get(profile_name(), {}).get("writable"))


def _candidates_available():
    """A ledger that predates migrate_task_candidates.sql still renders.

    The register is additive; a dashboard pointed at an older database
    should show one fewer section, not a 500.
    """
    try:
        q("SELECT 1 FROM v_task_candidates LIMIT 1")
        return True
    except psycopg2.Error:
        return False


def _attach_owner_options(proposals, viewer_ref):
    """Who a mis-attributed proposal could belong to instead.

    An owner correction is only easy if the right person is already in the
    list, and only defensible if the list is not a directory of everyone.
    So it is drawn from the conversation the claim came from: the people
    the extractor itself considered, whoever spoke in that window, and the
    viewer. Nobody outside that room is offered, which keeps a review
    control from doubling as a way to enumerate colleagues.

    Order carries meaning: the extractor's own candidates come first, so
    'it was actually the other person it considered' is the shortest move.
    """
    pids = [p["proposal_id"] for p in proposals]
    if not pids:
        return
    considered, authors, batches = {}, {}, {}
    for r in q("""
            SELECT ps.proposal_id, s.owner_candidates, s.author_ref,
                   s.batch_id
            FROM proposal_signals ps
            JOIN signals s ON s.signal_id = ps.signal_id
            WHERE ps.proposal_id = ANY(%s)""", (pids,)):
        seen = considered.setdefault(r["proposal_id"], [])
        for cand in (r["owner_candidates"] or []):
            ref = (cand or {}).get("person_ref")
            if ref and ref not in seen:
                seen.append(ref)
        if r["author_ref"]:
            authors.setdefault(r["proposal_id"], []).append(r["author_ref"])
        if r["batch_id"]:
            batches.setdefault(r["proposal_id"], set()).add(r["batch_id"])

    all_batches = sorted(set().union(*batches.values()) if batches else [])
    in_window, batch_series = {}, {}
    if all_batches:
        # "The room" is everyone the MEETING can vouch for: people who
        # SPOKE (author_ref) and people the meeting's claims ATTRIBUTED
        # work to (owner_candidates) -- across every sub-window of the
        # same meeting, not just this signal's batch. Two accidents had
        # narrowed it to one person. On the assistant ledger only
        # anchored claims carry a speaker, so one lucky anchor was the
        # sole correction on every card. On the real ledger, meetings
        # are split into 8-utterance batches for EXTRACTION quality
        # (windowing.py), and that split silently became a room
        # boundary: "who was in this conversation" was answered from a
        # three-minute slice, offering whoever happened to speak inside
        # it. Batches sharing a source_ref series (…-p1, …-p2) are one
        # meeting, so the room joins across them. Still never a
        # directory: nobody enters except through the meeting.
        for r in q("""
                SELECT batch_id,
                       regexp_replace(source_ref, '-p\\d+$', '') AS series
                FROM extraction_batches WHERE batch_id = ANY(%s)""",
                (all_batches,)):
            batch_series[r["batch_id"]] = r["series"]
        series_list = sorted(set(batch_series.values()))
        for r in q("""
                SELECT regexp_replace(b.source_ref, '-p\\d+$', '')
                         AS series,
                       s.author_ref, s.owner_candidates
                FROM extraction_batches b
                JOIN signals s USING (batch_id)
                WHERE regexp_replace(b.source_ref, '-p\\d+$', '')
                        = ANY(%s)""", (series_list,)):
            room = in_window.setdefault(r["series"], [])
            if r["author_ref"] and r["author_ref"] not in room:
                room.append(r["author_ref"])
            for cand in (r["owner_candidates"] or []):
                ref = (cand or {}).get("person_ref")
                if ref and ref not in room:
                    room.append(ref)

    for p in proposals:
        pid = p["proposal_id"]
        opts = list(considered.get(pid, []))
        for ref in authors.get(pid, []):
            if ref not in opts:
                opts.append(ref)
        for b in sorted(batches.get(pid, [])):
            for ref in in_window.get(batch_series.get(b), []):
                if ref not in opts:
                    opts.append(ref)
        if viewer_ref not in opts:
            opts.append(viewer_ref)
        p["owner_options"] = opts
        p["proposed_owner"] = (p["payload"] or {}).get("owner_ref")


def _room_for_candidates(v):
    # Same meeting-series scope as _attach_owner_options; see the room
    # comment there. b2 joins every sub-window of the meetings these
    # candidates were sighted in.
    rows = q("""
        SELECT DISTINCT s2.author_ref, s2.owner_candidates
        FROM task_candidate_events e
        JOIN signals s ON s.signal_id = e.signal_id
        JOIN extraction_batches b ON b.batch_id = s.batch_id
        JOIN extraction_batches b2
          ON regexp_replace(b2.source_ref, '-p\\d+$', '')
           = regexp_replace(b.source_ref, '-p\\d+$', '')
        JOIN signals s2 ON s2.batch_id = b2.batch_id
        JOIN v_task_candidates c
          ON c.candidate_id = e.candidate_id AND c.user_ref = e.user_ref
        WHERE e.user_ref = %s AND c.state = 'sighted'""", (v,))
    room = []
    for r in rows:
        refs = [r["author_ref"]] + [
            (cand or {}).get("person_ref")
            for cand in (r["owner_candidates"] or [])]
        for ref in refs:
            if ref and ref != v and ref not in room:
                room.append(ref)
    return sorted(room)


@app.route("/api/review")
def review_queue():
    """SELF ONLY. Unreviewed claims about a person, plus their quiet work."""
    v = viewer()
    if not v:
        return _need_viewer()
    packets = q("""
        WITH latest AS (SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT rp.packet_id, rp.generated_at, rp.board_version
        FROM review_packets rp JOIN latest l USING (packet_id)
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')
        ORDER BY rp.generated_at DESC LIMIT 1""", (v,))
    proposals = []
    if packets:
        proposals = q("""
            SELECT p.proposal_id, p.op, p.payload, p.confidence,
                   p.rationale, p.conflict_type, p.target_task_id,
                   t.title AS target_title, t.status AS target_status,
                   count(ev.resource_id) AS sources,
                   (count(*) FILTER (
                     WHERE ev.source_type = ANY(ARRAY['teams_message',
                       'email_message','transcript_utterance',
                       'document_span'])) > 0
                    OR NOT %s) AS traceable
            FROM proposals p
            LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
            LEFT JOIN tasks_current t ON t.task_id = p.target_task_id
            LEFT JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
            LEFT JOIN evidence_refs ev ON ev.signal_id = ps.signal_id
            WHERE p.packet_id = %s AND d.decision_id IS NULL
            GROUP BY p.proposal_id, p.op, p.payload, p.confidence,
                     p.rationale, p.conflict_type, p.target_task_id,
                     t.title, t.status
            ORDER BY (p.conflict_type IS NULL), p.confidence DESC""",
            (bool(PROFILES.get(profile_name(), {}).get("system_of_record")),
             packets[0]["packet_id"]))
        _attach_owner_options(proposals, v)
    # Quiet: open, mine, and nothing has happened to it in a week. Same
    # definition the board's "Gone quiet" filter uses, so the popup and the
    # page cannot disagree about what stale means.
    quiet = q("""
        SELECT t.task_id, t.title, t.status, t.deadline,
               max(e.occurred_at) AS last_event,
               (now()::date - max(e.occurred_at)::date) AS quiet_days
        FROM tasks_current t
        JOIN task_events e ON e.task_id = t.task_id
        WHERE t.user_ref = %s AND t.merged_into IS NULL
          AND t.status NOT IN ('completed','cancelled')
        GROUP BY t.task_id, t.title, t.status, t.deadline
        HAVING (now()::date - max(e.occurred_at)::date) >= %s
        ORDER BY max(e.occurred_at) ASC LIMIT 8""", (v, QUIET_DAYS))
    # Work referenced in conversation that is on nobody's board and has not
    # yet corroborated its way into a proposal. Showing every one would
    # recreate the noise the promotion rule exists to prevent, so this is
    # the shortlist: most-sighted first, capped. SELF ONLY like the rest of
    # this endpoint -- these are unreviewed claims about a person.
    candidates = q("""
        SELECT candidate_id, title, sightings, speakers, last_seen
        FROM v_task_candidates
        WHERE user_ref = %s AND state = 'sighted'
        ORDER BY sightings DESC, last_seen DESC
        LIMIT 6""", (v,)) if _candidates_available() else []
    # The register forgets on a clock (candidate_promotion.window_days) and
    # used to forget silently. A candidate about to fade carries its days
    # left, so the card can say "claim it or lose it" -- loss framed as
    # loss, not as an empty list next week.
    if candidates:
        try:
            import datetime as _dt
            import yaml as _yaml
            _root = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", ".."))
            with open(os.path.join(_root, "policy",
                                   "matching_policy.yaml")) as _f:
                _win = ((_yaml.safe_load(_f) or {}).get(
                    "candidate_promotion") or {}).get("window_days")
            if _win:
                _now = _dt.datetime.now(_dt.timezone.utc)
                for c in candidates:
                    seen = c.get("last_seen")
                    if isinstance(seen, str):   # q() hands out isoformat
                        seen = _dt.datetime.fromisoformat(seen)
                    if seen is not None:
                        c["days_left"] = max(0, _win - (_now - seen).days)
        except Exception as _exc:                       # noqa: BLE001
            import sys as _sys
            print("countdown skipped: %r" % (_exc,), file=_sys.stderr)
            # a missing countdown is never worth a 500

    # Changes that skipped the queue. If these were invisible the gate
    # would be indefensible: the whole basis for letting anything apply
    # itself is that you are told, and can put it back.
    auto = q("""
        SELECT e.event_id, e.task_id, t.title, e.occurred_at,
               e.payload->>'from_status' AS from_status,
               e.payload->>'to_status'   AS to_status,
               EXISTS (SELECT 1 FROM task_events u
                        WHERE u.task_id = e.task_id
                          AND u.actor_type = 'manual_edit'
                          AND u.board_version > e.board_version
                          AND u.payload->>'undoes_event_id'
                              = e.event_id::text) AS undone
        FROM task_events e
        JOIN tasks_current t USING (task_id)
        WHERE e.user_ref = %s AND e.actor_type = 'auto_applied'
          AND e.occurred_at > now() - interval '7 days'
        ORDER BY e.occurred_at DESC LIMIT 8""", (v,))

    return jsonify({
        "packet": packets[0] if packets else None,
        "proposals": proposals,
        "quiet": quiet,
        "quiet_days": QUIET_DAYS,
        "candidates": candidates,
        # The system's record WITH THIS PERSON, shown to them. A skeptic's
        # real question is "how often is this thing wrong" -- the honest
        # answer is a running count they generated themselves, corrections
        # included, because a system that hides its misses has decided
        # they matter less than its image.
        "answered": (q("""
            SELECT count(*) AS asked,
                   count(*) FILTER (WHERE decision = 'accept') AS accepted,
                   count(*) FILTER (WHERE decision = 'reject'
                     AND reject_reason IN ('wrong_owner',
                                           'wrong_task_match'))
                     AS corrected
            FROM decisions WHERE decided_by = %s""", (v,)) or [{}])[0],
        # What a fragment could be attached to. Open work only: nobody
        # files a new piece of work under something already finished.
        "my_tasks": q("""
            SELECT task_id, title FROM tasks_current
            WHERE user_ref = %s AND merged_into IS NULL
              AND status NOT IN ('completed', 'cancelled')
            ORDER BY title""", (v,)),
        # Who could own a fragment instead of the viewer: the same room
        # definition owner_options uses -- people who SPOKE in those
        # windows plus people the same batches ATTRIBUTED work to.
        # Speaker-only left this list at one person on an assistant
        # ledger, where only anchored claims carry a speaker.
        "room": _room_for_candidates(v),
        # THE BLOCKER MOVED. The schema has always stored cross-person
        # dependency edges -- blocking_task_id can point at a task on
        # somebody else's board -- and nothing ever watched them: the
        # other person finished, and the blocked person found out at the
        # next standup, or never. This is the single most connective
        # fact the ledger holds. Shown only while unanswered: any event
        # the person puts on their task after the blocker closed counts
        # as them knowing, so the card retires itself.
        "unblocked": q("""
            SELECT b.blocker_id, b.description,
                   t.task_id, t.title,
                   bt.title AS blocking_title,
                   bt.user_ref AS blocking_owner,
                   bt.status AS blocking_status,
                   ce.occurred_at AS closed_at, ce.user_ref AS closed_by
            FROM task_blockers b
            JOIN tasks_current t ON t.task_id = b.task_id
            JOIN tasks_current bt ON bt.task_id = b.blocking_task_id
            JOIN LATERAL (
              SELECT occurred_at, user_ref FROM task_events e
              WHERE e.task_id = bt.task_id
                AND e.event_type IN ('completed','cancelled')
              ORDER BY e.event_id DESC LIMIT 1) ce ON true
            WHERE t.user_ref = %s AND b.resolved_event_id IS NULL
              AND t.merged_into IS NULL
              AND t.status NOT IN ('completed','cancelled')
              AND bt.status IN ('completed','cancelled')
              AND NOT EXISTS (SELECT 1 FROM task_events e2
                              WHERE e2.task_id = t.task_id
                                AND e2.occurred_at > ce.occurred_at)
            ORDER BY ce.occurred_at DESC""", (v,)),
        # SAME WORK, TWO BOARDS? Two open tasks on different boards
        # with the same normalized title (or a shared taught alias) are
        # probably one piece of work -- the reconciler annotates these
        # at proposal time, but an annotation nobody must answer is an
        # annotation nobody reads. This is a card, to BOTH owners, and
        # either owner's ruling settles it for both, permanently.
        # Consent-scoped: a pair only shows when the other task is
        # already visible to this viewer through the grants system --
        # discovering a duplicate must not become a way to discover a
        # board you were never shown.
        "duplicates": _crossboard_duplicates(v),
        "no_blockers": _no_blocker_claims(v),
        "auto_applied": [a for a in auto if not a["undone"]],
        "writable": _writable(),
        "profile": profile_name(),
    })


_NO_BLOCKER_RX = None


def _no_blocker_patterns():
    """Compiled once, from the same policy file the reconciler reads.

    These titles are dropped by the gates -- see not_work in
    matching_policy.yaml -- because "no blockers" is not a task. But it is
    a true statement about somebody's work on a date, and discarding it
    entirely was the wrong half of a right decision. The signal is already
    stored; nothing new is written to report it.
    """
    global _NO_BLOCKER_RX
    if _NO_BLOCKER_RX is None:
        pats = []
        try:
            import re
            import yaml
            root = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", ".."))
            with open(os.path.join(root, "policy",
                                   "matching_policy.yaml")) as f:
                pats = (((yaml.safe_load(f) or {}).get("not_work") or {})
                        .get("attests_no_blockers") or [])
            _NO_BLOCKER_RX = [re.compile(x, re.IGNORECASE) for x in pats]
        except Exception:                               # noqa: BLE001
            _NO_BLOCKER_RX = []      # reporting a fact is never worth a 500
    return _NO_BLOCKER_RX


def _no_blocker_claims(v):
    """When this person last said nothing was blocked, and in what words."""
    rx = _no_blocker_patterns()
    if not rx:
        return []
    out = []
    for r in q("""
            SELECT s.title, b.window_start AS said_at, b.source_ref
            FROM signals s JOIN extraction_batches b USING (batch_id)
            WHERE s.author_ref = %s
              AND b.window_start >= now() - interval '30 days'
            ORDER BY b.window_start DESC
            LIMIT 400""", (v,)):
        if any(x.search(r["title"] or "") for x in rx):
            out.append(r)
            if len(out) >= 3:
                break
    return out


def _review_cli():
    """The terminal review loop's decision logic, reused verbatim.

    The browser must not grow its own copy of what a valid accept is: the
    op-to-event map, the optimistic-concurrency check and the ownerless-
    create refusal all live in cli/review.py and are imported, not
    reimplemented.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    pth = os.path.normpath(os.path.join(here, "..", "..", "cli"))
    if pth not in sys.path:
        sys.path.insert(0, pth)
    import review
    return review


def _write_conn():
    """A read-write session, used ONLY by the two decision endpoints.

    q() stays read-only for everything else. This is not a second way into
    the board: accepts are applied by cli/review.py's apply_accept(), which
    calls apply_task_event(), which refuses an actor_type of user_decision
    without a decision_id. What the browser adds is a caller, not a path.
    """
    return psycopg2.connect(profile_dsn())


@app.errorhandler(psycopg2.OperationalError)
def _unreachable_ledger(exc):
    """A ledger the server cannot reach is a SENTENCE, not a stack trace.

    Every panel carries the profile, so one bad DSN fails every panel at
    once: fourteen tracebacks in the console and fourteen boxes on the
    page reading "-> 500", none of which say the one thing that is
    wrong. The cause is nearly always a hand-typed TL_COPILOT_DSN --
    wrong role, wrong port, or a database that was never built -- and it
    is knowable here, from the exception Postgres already wrote.

    503, not 500: the request was fine, the ledger is not there.
    """
    parts = dict(kv.split("=", 1) for kv in profile_dsn().split()
                 if "=" in kv)
    where = "{0} on {1}:{2} as {3}".format(
        parts.get("dbname", "?"), parts.get("host", "?"),
        parts.get("port", "?"), parts.get("user", "?"))
    msg = " ".join(str(exc).split())
    if "does not exist" in msg and "role" in msg:
        why = ("that Postgres has no role by that name -- on Windows the "
               "role is your own username, not 'postgres'")
    elif "does not exist" in msg and "database" in msg:
        why = ("that database does not exist yet -- build it with "
               "\"tl copilot <claims file> --apply\"")
    elif "Connection refused" in msg or "could not connect" in msg:
        why = "nothing is listening there -- is Postgres running?"
    else:
        why = msg
    env = {"copilot": "TL_COPILOT_DSN", "synthetic": "TL_SYNTH_DSN"}.get(
        profile_name(), "DB_DSN")
    return jsonify({
        "error": "the '{0}' ledger is not reachable".format(profile_name()),
        "detail": "Tried {0}: {1}. Fix {2} and reload.".format(
            where, why, env)}), 503


def _refuse_write():
    return jsonify({
        "error": "this ledger is read-only from the browser",
        "detail": "Decisions are recorded here only on the synthetic "
                  "profile. The real ledger holds colleague data and its "
                  "dashboard session is read-only by design -- decide "
                  "there through the Teams card or cli/review.py."}), 403


# Evidence source_types that point at something a person can go and read.
# 'assistant_claim' is deliberately absent: it is the marker written when
# an assistant asserted a transcript supports it but produced no locator.
CITABLE_SOURCES = ("teams_message", "email_message", "transcript_utterance",
                   "document_span")


def _unacceptable(cur, proposal_id):
    """Reason this proposal cannot be ACCEPTED from a browser, or None.

    Anchoring made the copilot ledger a MIXED one: some claims quoted a
    sentence we found and now cite it by id and hash, others quoted
    something no transcript contains and still cite nobody. Gating that
    at the profile level can only be wrong in one of two directions --
    lock the whole ledger and the anchoring was cosmetic, open it and a
    made-up sentence becomes acceptable with one click.

    So the gate is per card, and it asks the only question that matters:
    is there evidence here a person could go and read? Applies to every
    profile, because the answer should not depend on which ledger the
    claim happens to be sitting in.

    REVISED TWICE, and both revisions narrowed it.

    First it gated rejection too, reasoning that "no" is a judgement
    which trains the eval and therefore needs something real to be
    about. That does not survive the two decisions being different:
    accepting puts work on a board on the strength of a claim nobody can
    check, and rejecting declines to. Declining an unverifiable claim is
    safe, and it is the most informative thing a reviewer can say about
    one.

    Then it applied to every ledger, on the reasoning that the answer
    should not depend on which ledger a claim sits in. But the LEDGERS
    are not alike, and pretending otherwise cost the thing the second
    ledger exists for. A system of record is the answer to "what am I
    working on", and a board carrying items that trace to nothing is
    exactly the failure this whole design is against. A comparison
    ledger is an instrument: its board is disposable, and its entire
    purpose is to record what a person thought of each card. Blocking
    accept there protects nothing and destroys the measurement -- worse,
    it makes one arm of a blind comparison structurally unable to accept
    anything, so the arms differ by rule rather than by merit and the
    result means nothing.

    So: gated on a system of record, open on an instrument. The evidence
    type is written down either way, so a task created from an
    uncitable claim can always be identified as one afterwards.
    """
    if not PROFILES.get(profile_name(), {}).get("system_of_record"):
        return None
    cur.execute("""
        SELECT count(*) FILTER (WHERE ev.source_type = ANY(%s)) AS citable,
               count(*) AS total
        FROM proposal_signals ps
        JOIN evidence_refs ev ON ev.signal_id = ps.signal_id
        WHERE ps.proposal_id = %s""", (list(CITABLE_SOURCES), proposal_id))
    row = cur.fetchone()
    citable = (row["citable"] if isinstance(row, dict) else row[0]) or 0
    if citable:
        return None
    return ("Nothing on this card can be traced to a sentence. An "
            "assistant asserted it and released no utterance to check it "
            "against, so it cannot be put on a board from here. You can "
            "still say no to it -- declining a claim nobody can check is "
            "exactly what should happen to one.")


@app.route("/api/decide", methods=["POST"])
def decide():
    """Record a human decision on one proposal. SELF ONLY, synthetic only."""
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    proposal_id = (body.get("proposal_id") or "").strip()
    choice = (body.get("decision") or "").strip()
    if choice not in ("accept", "reject", "defer"):
        return jsonify({"error": "decision must be accept, reject or "
                                 "defer"}), 400
    reason = (body.get("reject_reason") or "").strip() or None
    if choice == "reject" and not reason:
        return jsonify({"error": "a rejection needs a reason -- that is "
                                 "the signal the eval set is built "
                                 "from"}), 400

    review_cli = _review_cli()
    conn = _write_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # SELF ONLY, enforced against the packet's owner rather than
            # anything the request claims.
            cur.execute("""
                SELECT p.*, rp.user_ref, rp.packet_id,
                       t.title AS target_title, t.status AS target_status,
                       t.merged_into AS target_merged
                FROM proposals p
                JOIN review_packets rp USING (packet_id)
                LEFT JOIN tasks_current t ON t.task_id = p.target_task_id
                LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
                WHERE p.proposal_id = %s AND d.decision_id IS NULL""",
                (proposal_id,))
            prop = cur.fetchone()
            if not prop:
                return jsonify({"error": "no such undecided proposal"}), 404
            if prop["user_ref"] != v:
                return jsonify({"error": "that packet is not yours"}), 403
            # After the request has been checked and before anything is
            # recorded: a malformed request is wrong whatever card it
            # names, and this refusal is about the card. ACCEPT only --
            # see _unacceptable(). Saying no to a claim nobody can check
            # is not a thing to prevent, it is the thing to want.
            if choice == "accept":
                why = _unacceptable(cur, proposal_id)
                if why:
                    return jsonify({"error": "this card cannot be "
                                             "accepted from the browser",
                                    "detail": why}), 403

            if choice == "defer":
                review_cli.defer_signals(cur, v, prop)
                cur.execute("""
                    INSERT INTO decisions (proposal_id, packet_id, decision,
                      decided_by, decided_at)
                    VALUES (%s,%s,'defer',%s,now())
                    ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                    (proposal_id, prop["packet_id"], v))
                conn.commit()
                return jsonify({"ok": True, "decision": "defer"})

            if choice == "reject":
                # "Not mine -- it is theirs" is still a rejection, and the
                # reason is still wrong_owner, which is what the attribution
                # eval counts. What reassign_to adds is where the claim goes
                # next: back into the pool addressed to that person, so it
                # reaches them as a proposal in their own packet. Their board
                # still only moves when they decide. Nothing here writes to
                # anyone else's board, which is the whole reason it re-pools
                # instead of creating the task outright.
                to = (body.get("reassign_to") or "").strip()
                if to:
                    if reason != "wrong_owner":
                        return jsonify({"error": "a reassignment is a "
                                                 "wrong_owner rejection"}), 400
                    if to == v:
                        return jsonify({"error": "that is you -- accept it "
                                                 "instead"}), 400
                    # Only someone who was in the conversation this claim
                    # came from. Correcting attribution must not double as a
                    # way to put work on any colleague you can name.
                    probe = [{"proposal_id": proposal_id,
                              "payload": prop["payload"]}]
                    _attach_owner_options(probe, v)
                    if to not in probe[0]["owner_options"]:
                        return jsonify({"error": "that person was not in the "
                                                 "conversation this claim "
                                                 "came from"}), 400
                cur.execute("""
                    INSERT INTO decisions (proposal_id, packet_id, decision,
                      reject_reason, decided_by, decided_at)
                    VALUES (%s,%s,'reject',%s,%s,now())
                    ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                    (proposal_id, prop["packet_id"], reason, v))
                repooled = 0
                if to:
                    # Pooled FOR them, not returned to me: I have ruled on
                    # this claim, and my ruling was that it is theirs.
                    cur.execute("""
                        INSERT INTO signal_pool_events
                          (signal_id, user_ref, event, run_id)
                        SELECT ps.signal_id, %s, 'pooled', 'review-reassign'
                        FROM proposal_signals ps
                        WHERE ps.proposal_id = %s""", (to, proposal_id))
                    repooled = cur.rowcount
                conn.commit()
                return jsonify({"ok": True, "decision": "reject",
                                "reject_reason": reason,
                                "reassigned_to": to or None,
                                "signals_repooled": repooled})

            ok, why = review_cli.still_valid(prop)
            if not ok:
                # The board moved under the proposal; recording it anyway
                # would apply a change to a state nobody reviewed.
                cur.execute("""
                    INSERT INTO decisions (proposal_id, packet_id, decision,
                      decided_by, decided_at, invalidated_reason)
                    VALUES (%s,%s,'accept',%s,now(),%s)
                    ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                    (proposal_id, prop["packet_id"], v, why))
                conn.commit()
                return jsonify({"ok": False, "invalidated": why})

            payload = dict(prop["payload"] or {})
            if review_cli.needs_owner(prop):
                payload["owner_ref"] = body.get("owner_ref") or v
            cur.execute("""
                INSERT INTO decisions (proposal_id, packet_id, decision,
                  decided_by, decided_at)
                VALUES (%s,%s,'accept',%s,now())
                ON CONFLICT (proposal_id, decided_by) DO NOTHING
                RETURNING decision_id""",
                (proposal_id, prop["packet_id"], v))
            if cur.fetchone() is None:
                return jsonify({"error": "already decided"}), 409
            task_id = review_cli.apply_accept(cur, v, prop, payload)
            conn.commit()
            return jsonify({"ok": True, "decision": "accept",
                            "task_id": task_id})
    finally:
        conn.close()


@app.route("/api/undo-auto", methods=["POST"])
def undo_auto():
    """Put back a change nobody approved.

    A compensating event, not a deletion -- task_events is append-only and
    the fact that this was applied automatically stays true. The revert is
    actor_type='manual_edit' because reverting IS a person typing
    something, and it carries undoes_event_id so the pair is legible from
    either end.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    try:
        event_id = int(body.get("event_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "event_id required"}), 400

    conn = _write_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT event_id, task_id, user_ref, actor_type,
                       payload->>'from_status' AS from_status,
                       payload->>'to_status'   AS to_status
                FROM task_events
                WHERE event_id = %s""", (event_id,))
            ev = cur.fetchone()
            if not ev or ev["actor_type"] != "auto_applied":
                return jsonify({"error": "no such auto-applied event"}), 404
            if ev["user_ref"] != v:
                return jsonify({"error": "that is not your board"}), 403
            if not ev["from_status"]:
                return jsonify({"error": "nothing to revert to"}), 400
            cur.execute(
                "SELECT apply_task_event(%s,%s,'status_changed','manual_edit',"
                "NULL,%s::jsonb)",
                (ev["task_id"], v, json.dumps({
                    "from_status": ev["to_status"],
                    "to_status": ev["from_status"],
                    "undoes_event_id": str(event_id),
                })))
            conn.commit()
            return jsonify({"ok": True, "task_id": ev["task_id"],
                            "status": ev["from_status"]})
    finally:
        conn.close()


@app.route("/api/candidate", methods=["POST"])
def candidate_rule():
    """Rule on work the ledger has heard about but nobody has tracked.

    Two answers, and they are not symmetrical. "Not a task" writes only a
    dismissal event -- no board changes, and the register stops offering it,
    which is the whole point of asking. "Track it" creates the task with
    actor_type='manual_edit' and a NULL decision_id, the same path
    /api/nudge uses: a person typed this, and that stays distinguishable
    from "a model proposed it and a person agreed" forever.

    SELF ONLY, and synthetic-only for writes, like every other write here.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    cid = (body.get("candidate_id") or "").strip()
    action = (body.get("action") or "").strip()
    if action not in ("track", "dismiss", "attach", "reassign"):
        return jsonify({"error": "action must be track, dismiss, attach "
                                 "or reassign"}), 400

    conn = _write_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT candidate_id, title, state FROM v_task_candidates
                WHERE candidate_id = %s AND user_ref = %s""", (cid, v))
            cand = cur.fetchone()
            if not cand:
                return jsonify({"error": "no such candidate"}), 404
            if cand["state"] != "sighted":
                return jsonify({"error": "already ruled on"}), 409

            # "Not mine -- it is theirs." The extractor deliberately
            # returns NO owner for an impersonal sentence, and the pool
            # then routes it to whoever said it, which is right for
            # asking and wrong for owning. Until now the only ways out
            # were to accept it (making it yours by clicking a button --
            # the attribution error people actually complained about) or
            # dismiss it (throwing away real work). Proposals have had
            # this third way for a while; candidates never did.
            #
            # It re-pools rather than writing to their board: nobody's
            # board moves except by their own decision, so this reaches
            # them as their own card to rule on.
            if action == "reassign":
                to = (body.get("to") or "").strip()
                if not to or to == v:
                    return jsonify({"error": "name whose it is, and not "
                                             "yourself"}), 400
                # Same room the dropdown was built from: speakers AND
                # people the batch attributed work to. A validator
                # stricter than the list it validates rejects choices
                # the UI itself offered.
                cur.execute("""
                    SELECT DISTINCT s2.author_ref, s2.owner_candidates
                    FROM task_candidate_events e
                    JOIN signals s ON s.signal_id = e.signal_id
                    JOIN extraction_batches b ON b.batch_id = s.batch_id
                    JOIN extraction_batches b2
                      ON regexp_replace(b2.source_ref, '-p\\d+$', '')
                       = regexp_replace(b.source_ref, '-p\\d+$', '')
                    JOIN signals s2 ON s2.batch_id = b2.batch_id
                    WHERE e.candidate_id = %s AND e.user_ref = %s""",
                    (cid, v))
                inroom = set()
                for r in cur.fetchall():
                    if r["author_ref"]:
                        inroom.add(r["author_ref"])
                    for cand in (r["owner_candidates"] or []):
                        if (cand or {}).get("person_ref"):
                            inroom.add(cand["person_ref"])
                if to not in inroom:
                    # Correcting attribution must not double as a way to
                    # put work on any colleague you can name.
                    return jsonify({"error": "that person was not part of "
                                             "the conversation this came "
                                             "from"}), 400
                cur.execute("""
                    SELECT DISTINCT signal_id FROM task_candidate_events
                    WHERE candidate_id = %s AND user_ref = %s
                      AND signal_id IS NOT NULL""", (cid, v))
                moved = 0
                for row in cur.fetchall():
                    cur.execute("""
                        INSERT INTO signal_pool_events
                          (signal_id, user_ref, event, run_id)
                        VALUES (%s,%s,'pooled','dashboard-reassign')""",
                        (row["signal_id"], to))
                    moved += 1
                cur.execute("""
                    INSERT INTO task_candidate_events
                      (candidate_id, user_ref, event, title, run_id)
                    VALUES (%s,%s,'dismissed',%s,'dashboard-reassign')""",
                    (cid, v, cand["title"]))
                conn.commit()
                return jsonify({"ok": True, "action": "reassign",
                                "to": to, "signals_repooled": moved})

            if action == "dismiss":
                cur.execute("""
                    INSERT INTO task_candidate_events
                      (candidate_id, user_ref, event, title, run_id)
                    VALUES (%s,%s,'dismissed',%s,'dashboard')""",
                    (cid, v, cand["title"]))
                conn.commit()
                return jsonify({"ok": True, "action": "dismiss"})

            # ATTACH: this is a piece of something bigger.
            #
            # The register is full of fragments -- "finish what was
            # discussed yesterday", "count events per day" -- that are
            # real work described in the shorthand people use when the
            # context is obvious to everyone in the room. Standing alone
            # they are useless; under the project they belong to they are
            # exactly right. The system cannot always infer the parent,
            # but the person reading the card always knows it, so this is
            # the one keystroke that lets them say so: pick an existing
            # task, or name a new parent that does not exist yet.
            parent_id = (body.get("parent_task_id") or "").strip() or None
            parent_title = (body.get("parent_title") or "").strip() or None
            if action == "attach" and not (parent_id or parent_title):
                return jsonify({"error": "attach needs parent_task_id or "
                                         "parent_title"}), 400
            if action == "attach" and parent_id:
                cur.execute("""SELECT task_id FROM tasks_current
                               WHERE task_id = %s AND user_ref = %s""",
                            (parent_id, v))
                if not cur.fetchone():
                    return jsonify({"error": "no such task on your "
                                             "board"}), 404
            if action == "attach" and not parent_id:
                # A named parent nobody has tracked yet: create it, then
                # hang this under it. Two tasks from one keystroke, which
                # is the shape of the thought being expressed.
                parent_id = "task-p" + cid[:7]
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                    "NULL,%s::jsonb)",
                    (parent_id, v, json.dumps({
                        "title": parent_title[:120], "description": None,
                        "status": "proposed", "owner_ref": v,
                        "deadline": None, "parent_task_id": None})))

            task_id = "task-" + cid[:8]
            # The heard claim rides in the payload. Without it, a task a
            # person ACCEPTED from a meeting claim was labeled "typed by
            # hand" everywhere -- the first pilot reviewer said, flatly,
            # that he did not type it, and he was right. actor_type stays
            # manual_edit (a human decided, no proposal id exists on the
            # candidate path); the payload says what the decision was
            # ABOUT, and every surface can now say "accepted from a
            # claim heard in <meeting>" instead of lying by omission.
            cur.execute("""
                SELECT e.signal_id, b.source_ref
                FROM task_candidate_events e
                JOIN signals s ON s.signal_id = e.signal_id
                JOIN extraction_batches b ON b.batch_id = s.batch_id
                WHERE e.candidate_id = %s AND e.user_ref = %s
                  AND e.event = 'sighted' AND e.signal_id IS NOT NULL
                ORDER BY e.event_id DESC LIMIT 1""", (cid, v))
            lead = cur.fetchone()
            cur.execute(
                "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                "NULL,%s::jsonb)",
                (task_id, v, json.dumps({
                    "title": cand["title"], "description": None,
                    "status": "proposed", "owner_ref": v,
                    "deadline": None,
                    "parent_task_id": parent_id
                    if action == "attach" else None,
                    "accepted_from_signal": (lead["signal_id"]
                                             if lead else None),
                    "heard_in": (lead["source_ref"] if lead else None)})))
            # Every wording the register heard this work under becomes an
            # alias, so the paraphrases that corroborated the candidate can
            # match the task directly instead of asking a judge -- the same
            # carry-over cli/review.py does on accept.
            cur.execute("""
                SELECT DISTINCT ON (title) title, signal_id
                FROM task_candidate_events
                WHERE candidate_id = %s AND user_ref = %s
                  AND event = 'sighted' AND title <> %s
                ORDER BY title, event_id DESC""", (cid, v, cand["title"]))
            for alias, sid in [(r["title"], r["signal_id"])
                               for r in cur.fetchall()]:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'alias_added','system',"
                    "NULL,%s::jsonb)",
                    (task_id, v, json.dumps(
                        {"alias": alias, "source_signal_id": sid})))
            cur.execute("""
                INSERT INTO task_candidate_events
                  (candidate_id, user_ref, event, title, run_id)
                VALUES (%s,%s,'accepted',%s,'dashboard')""",
                (cid, v, cand["title"]))
            conn.commit()
            return jsonify({"ok": True, "action": action,
                             "task_id": task_id,
                             "parent_task_id": parent_id
                             if action == "attach" else None})
    finally:
        conn.close()


# ---------------------------------------------------------------- workload
#
# The sheet that answers "what is my week going to": per project, a
# priority, cumulative hours, this week's plan, and the projected
# end-of-week total. Modelled on the spreadsheet its first user already
# keeps by hand -- the columns are that sheet's columns.
#
# THE RULE THAT SHAPES ALL OF IT: three kinds of number, never blended.
#   stated    the person typed it (capacity, opening totals, priorities,
#             an edited plan) -- persisted in workload_events
#   measured  computed from evidence the system can show (meeting
#             minutes summed from transcript window timestamps) --
#             recomputed on read, carrying what it was measured from
#   proposed  the system's suggestion -- NEVER persisted, recomputed on
#             every read, dies on screen unless the person saves it,
#             at which point it becomes stated because they said so
# A cell always says which kind it is. An hours number of unknown
# provenance is exactly as corrosive here as an unattributed task.

def _monday(d=None):
    d = d or datetime.date.today()
    return d - datetime.timedelta(days=d.weekday())


# The estimate parser: the team's spoken conventions, converted at the
# policy rate. Deterministic regex over text people typed or accepted
# -- no model, so a derived hour can always name the phrase it came
# from. One point = half a working day; the rate lives in
# matching_policy.yaml (estimation.story_point_hours) and NOWHERE else.
_EST_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*(?:story\s*)?(?:points?|pts?|sp)\b"
    r"|\b(half\s+a\s+day|a\s+full\s+day|a\s+day)\b"
    r"|(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b)", re.IGNORECASE)


def _sp_hours():
    try:
        import yaml as _y
        root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        with open(os.path.join(root, "policy",
                               "matching_policy.yaml")) as f:
            return float(((_y.safe_load(f) or {}).get("estimation")
                          or {}).get("story_point_hours", 3.5))
    except Exception:                                   # noqa: BLE001
        return 3.5


def parse_estimate(text):
    """(hours, the phrase it came from) or (None, None)."""
    m = _EST_RE.search(text or "")
    if not m:
        return None, None
    rate = _sp_hours()
    if m.group(1):                      # N points / sp / pts
        return float(m.group(1)) * rate, m.group(0).strip()
    if m.group(2):                      # half a day / a day
        word = m.group(2).lower()
        return (rate if "half" in word else rate * 2), m.group(0).strip()
    return float(m.group(3)), m.group(0).strip()


def _workload_subjects(v):
    """The sheet's rows: this person's projects, their top-level parent
    tasks (a parent with children IS a project in all but name), and
    standalone open tasks. Each with the board facts the sheet shows:
    active/blocked, start date, last movement."""
    rows = q("""
        WITH mine AS (
          SELECT t.task_id, t.title, t.status, t.project_id,
                 t.parent_task_id, t.created_at, t.updated_at
          FROM tasks_current t
          WHERE t.user_ref = %s AND t.merged_into IS NULL),
        grouped AS (
          SELECT COALESCE(p.project_id, par.task_id, m.task_id)
                   AS subject,
                 COALESCE(p.name, par.title, m.title) AS label,
                 min(m.created_at) AS start_date,
                 max(m.updated_at) AS last_moved,
                 bool_and(m.status IN ('completed','cancelled')) AS closed
          FROM mine m
          LEFT JOIN projects p ON p.project_id = m.project_id
          LEFT JOIN mine par ON par.task_id = m.parent_task_id
          GROUP BY 1, 2)
        SELECT g.*,
               (SELECT b.description FROM task_blockers b
                 JOIN mine mm ON mm.task_id = b.task_id
                 WHERE b.resolved_event_id IS NULL
                   AND COALESCE(mm.project_id, mm.parent_task_id,
                                mm.task_id) = g.subject
                 ORDER BY b.blocker_id LIMIT 1) AS blocker
        FROM grouped g""", (v,))
    out = [dict(r) for r in rows if not r["closed"]]
    out.sort(key=lambda r: str(r["label"]).lower())
    return out


def _meeting_minutes(v, subjects, weeks_back=3):
    """MEASURED: minutes of meeting per subject per week, from the
    transcript windows' own timestamps.

    A meeting is attributed to a subject when a signal extracted from it
    is traceably attached to one of the subject's tasks -- through the
    aliases the reviewer's accepts taught, or the proposals they ruled
    on. A meeting attached to several subjects splits its minutes evenly
    between them: crude, said openly in the basis, and honest -- the
    alternative is inventing a weighting nobody measured. Meetings that
    touch none of this person's work count toward their meeting load but
    no project's."""
    ids = [s["subject"] for s in subjects]
    rows = q("""
        WITH linked AS (
          SELECT s.batch_id,
                 COALESCE(t.project_id, t.parent_task_id, t.task_id)
                   AS subject
          FROM task_aliases a
          JOIN tasks_current t USING (task_id)
          JOIN signals s ON s.signal_id = a.source_signal_id
          WHERE t.user_ref = %s
          UNION
          SELECT s.batch_id,
                 COALESCE(t.project_id, t.parent_task_id, t.task_id)
          FROM decisions d
          JOIN proposals pr ON pr.proposal_id = d.proposal_id
          JOIN proposal_signals ps ON ps.proposal_id = pr.proposal_id
          JOIN signals s ON s.signal_id = ps.signal_id
          JOIN task_events te ON te.decision_id = d.decision_id
          JOIN tasks_current t ON t.task_id = te.task_id
          WHERE t.user_ref = %s)
        SELECT regexp_replace(b.source_ref, '-p\\d+$', '') AS series,
               date_trunc('week', min(b.window_start))::date AS week_start,
               GREATEST(1, EXTRACT(epoch FROM
                 max(b.window_end) - min(b.window_start)) / 60)::int
                 AS minutes,
               array_agg(DISTINCT l.subject) FILTER
                 (WHERE l.subject = ANY(%s)) AS subjects
        FROM extraction_batches b
        LEFT JOIN linked l ON l.batch_id = b.batch_id
        WHERE b.source_type = 'meeting_transcript'
          AND b.window_start > now() - make_interval(weeks => %s)
        GROUP BY 1""", (v, v, ids, weeks_back))
    per = {}          # (subject, week) -> [minutes, [series...]]
    load = {}         # week -> total meeting minutes for the person
    for r in rows:
        wk = r["week_start"]
        if isinstance(wk, str):
            wk = datetime.date.fromisoformat(wk[:10])
        load[wk] = load.get(wk, 0) + r["minutes"]
        subs = r["subjects"] or []
        for s in subs:
            key = (s, wk)
            slot = per.setdefault(key, [0.0, []])
            slot[0] += r["minutes"] / len(subs)
            slot[1].append(r["series"])
    return per, load


def _stated(v, week):
    """Stated numbers for the week -- or None when this ledger predates
    migrate_workload.sql. None, not {}: an empty week and a missing
    table are different facts, and the sheet says which. A kit's demo
    database is restored from a dump made before the migration, so this
    is the normal state of every kit in the field, not an edge case."""
    try:
        rows = q("""
            SELECT subject, kind, hours, priority, basis
            FROM v_workload_current
            WHERE user_ref = %s AND week_start = %s""", (v, week))
    except psycopg2.errors.UndefinedTable:
        return None
    out = {}
    for r in rows:
        out[(r["subject"], r["kind"])] = r
    return out


def _propose_priorities(subjects, stated, deadlines, activity):
    """PROPOSED priority where none is stated: blocked work last (their
    sheet does exactly this), then nearest deadline, then most recent
    movement. A guess, labeled as one, one click to overrule."""
    def sort_key(s):
        return (1 if s.get("blocker") else 0,
                deadlines.get(s["subject"]) or datetime.date.max,
                -(activity.get(s["subject"]) or 0))
    ranked = sorted(subjects, key=sort_key)
    out = {}
    nxt = 1
    for s in ranked:
        st = stated.get((s["subject"], "priority"))
        out[s["subject"]] = ({"value": int(st["priority"]),
                              "source": "stated"} if st else
                             {"value": nxt, "source": "proposed"})
        nxt += 1
    return out


def _propose_plan(capacity, meeting_hours, subjects, priorities, stated,
                  estimates=None):
    """PROPOSED hours for a week. Estimates outrank arithmetic: a
    subject whose open tasks carry a spoken estimate ("3 points", at
    the policy rate) is proposed those hours -- a number someone
    actually said beats a share computed from nothing. If estimates
    overcommit the focus that remains after meetings, they scale down
    proportionally rather than silently fitting; what focus is left
    after estimated subjects splits across the rest by priority weight.
    Blocked rows get 0 regardless -- an estimate on work that cannot
    move is still work that cannot move. All of it dies unless saved.
    """
    estimates = estimates or {}
    plan = {}
    open_rows = [s for s in subjects if not s.get("blocker")]
    focus = max(0.0, (capacity or 0.0) - meeting_hours)
    est_rows = [s for s in open_rows
                if estimates.get(s["subject"], (None,))[0]]
    est_total = sum(estimates[s["subject"]][0] for s in est_rows)
    scale = (focus / est_total if est_total > focus and est_total
             else 1.0)
    rest = [s for s in open_rows if s not in est_rows]
    weights = {s["subject"]: 1.0 / priorities[s["subject"]]["value"]
               for s in rest}
    wsum = sum(weights.values()) or 1.0
    leftover = max(0.0, focus - min(est_total, focus))
    for s in subjects:
        sid = s["subject"]
        st = stated.get((sid, "planned"))
        if st is not None:
            plan[sid] = {"value": float(st["hours"]), "source": "stated"}
        elif s.get("blocker"):
            plan[sid] = {"value": 0.0, "source": "proposed"}
        elif sid in [x["subject"] for x in est_rows]:
            v = estimates[sid][0] * scale
            plan[sid] = {"value": round(v * 2) / 2, "source": "proposed",
                         "basis": "estimate"}
        else:
            share = leftover * weights.get(sid, 0) / wsum
            plan[sid] = {"value": round(share * 2) / 2,
                         "source": "proposed"}
    return plan


@app.route("/api/workload")
def workload():
    """SELF ONLY: the sheet is a person's own week."""
    v = viewer()
    if not v:
        return _need_viewer()
    try:
        week = _monday(datetime.date.fromisoformat(
            request.args.get("week", "")))
    except ValueError:
        week = _monday()
    subjects = _workload_subjects(v)
    per, load = _meeting_minutes(v, subjects)
    stated = _stated(v, week)
    savable = stated is not None
    if stated is None:
        stated = {}

    deadlines = {r["subj"]: r["dl"] for r in q("""
        SELECT COALESCE(project_id, parent_task_id, task_id) AS subj,
               min(deadline) AS dl
        FROM tasks_current WHERE user_ref = %s AND deadline IS NOT NULL
          AND merged_into IS NULL
          AND status NOT IN ('completed','cancelled')
        GROUP BY 1""", (v,))}
    for k, val in list(deadlines.items()):
        if isinstance(val, str):
            deadlines[k] = datetime.date.fromisoformat(val[:10])
    activity = {r["subj"]: r["n"] for r in q("""
        SELECT COALESCE(t.project_id, t.parent_task_id, t.task_id)
                 AS subj, count(*) AS n
        FROM task_events e JOIN tasks_current t USING (task_id)
        WHERE t.user_ref = %s
          AND e.occurred_at > now() - interval '14 days'
        GROUP BY 1""", (v,))}

    cap_row = stated.get(("_capacity", "capacity"))
    capacity = (float(cap_row["hours"]) if cap_row else None)
    # Projected meeting load for THIS week: the mean of the measured
    # weeks -- recurring meetings recur; said in the label.
    week_loads = [m for w, m in load.items() if w < week]
    meeting_proj = (sum(week_loads) / len(week_loads) / 60.0
                    if week_loads else 0.0)
    priorities = _propose_priorities(subjects, stated, deadlines, activity)
    # Spoken estimates on this person's open tasks, found by the
    # deterministic parser in title or description ("Estimate: 3
    # points." travels there from a Copilot claim; a typed note or an
    # accepted card carries its own words). Summed per subject; each
    # derived hour keeps the phrase it came from.
    estimates = {}
    for r in q("""
            SELECT COALESCE(project_id, parent_task_id, task_id) AS subj,
                   title, description
            FROM tasks_current
            WHERE user_ref = %s AND merged_into IS NULL
              AND status NOT IN ('completed','cancelled')""", (v,)):
        hrs, phrase = parse_estimate(
            (r["title"] or "") + " " + (r["description"] or ""))
        if hrs:
            cur = estimates.setdefault(r["subj"], [0.0, []])
            cur[0] += hrs
            cur[1].append(phrase)
    plan = _propose_plan(capacity, meeting_proj, subjects, priorities,
                         stated, estimates)

    rows = []
    for s in subjects:
        sid = s["subject"]
        opening = stated.get((sid, "opening_total"))
        meet = [(w, m) for (subj, w), (m, ser) in per.items()
                if subj == sid]
        measured_h = round(sum(m for _w, m in meet) / 60.0, 1)
        basis = sorted({ser for (subj, _w), (_m, sers) in per.items()
                        if subj == sid for ser in sers})
        opening_v = float(opening["hours"]) if opening else 0.0
        rows.append({
            "subject": sid, "label": s["label"],
            "active": not s.get("blocker"),
            "blocker": s.get("blocker"),
            "start_date": str(s["start_date"])[:10],
            "last_moved": str(s["last_moved"])[:10],
            "priority": priorities[sid],
            "opening_total": {"value": opening_v,
                              "source": "stated" if opening else "unset"},
            "meeting_hours_measured": {"value": measured_h,
                                       "basis": basis,
                                       "source": "measured"},
            "planned": plan[sid],
            "estimate": ({"hours": round(estimates[sid][0], 1),
                          "phrases": estimates[sid][1]}
                         if sid in estimates else None),
            "eow_total": round(opening_v + plan[sid]["value"], 1),
        })
    rows.sort(key=lambda r: r["priority"]["value"])
    return jsonify({
        "week_start": str(week),
        "capacity": {"value": capacity,
                     "source": "stated" if cap_row else "unset"},
        "meeting_hours_projected": round(meeting_proj, 1),
        "story_point_hours": _sp_hours(),
        "rows": rows,
        "savable": savable,
        "shared_with": [r["grantee_ref"] for r in q("""
            SELECT DISTINCT grantee_ref FROM v_current_grants
            WHERE granted_by = %s AND subject_type = 'user'
              AND subject_id = %s AND level = 'workload'""", (v, v))],
        "note": ("Numbers marked proposed are suggestions and are not "
                 "stored; saving is what makes them yours."
                 if savable else
                 "This ledger predates the workload table, so the sheet "
                 "is read-only: proposals show, nothing can be saved. "
                 "Apply storage/migrate_workload.sql to turn saving on."),
    })


@app.route("/api/workload", methods=["POST"])
def workload_save():
    """SELF ONLY, writable profiles only: the person stating facts about
    their own week. Everything saved here is 'stated' by definition --
    the act of saving is the person agreeing."""
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    try:
        week = _monday(datetime.date.fromisoformat(
            (body.get("week_start") or "")))
    except ValueError:
        return jsonify({"error": "week_start must be a date"}), 400
    valid = {s["subject"] for s in _workload_subjects(v)}
    events = []
    if body.get("capacity") is not None:
        events.append(("_capacity", "capacity",
                       float(body["capacity"]), None))
    for r in (body.get("rows") or []):
        sid = (r.get("subject") or "").strip()
        if sid not in valid:
            return jsonify({"error": "not a row on your sheet: "
                                     + sid[:60]}), 400
        if r.get("planned") is not None:
            events.append((sid, "planned", float(r["planned"]), None))
        if r.get("opening_total") is not None:
            events.append((sid, "opening_total",
                           float(r["opening_total"]), None))
        if r.get("priority") is not None:
            events.append((sid, "priority", None, int(r["priority"])))
    if not events:
        return jsonify({"error": "nothing to save"}), 400
    conn = _write_conn()
    try:
        with conn.cursor() as cur:
            for subject, kind, hours, prio in events:
                cur.execute("""
                    INSERT INTO workload_events (user_ref, subject,
                      week_start, kind, hours, priority, source)
                    VALUES (%s,%s,%s,%s,%s,%s,'stated')""",
                    (v, subject, week, kind, hours, prio))
        conn.commit()
    except psycopg2.errors.UndefinedTable:
        return jsonify({"error": "this ledger predates the workload "
                                 "table",
                        "detail": "Apply storage/migrate_workload.sql "
                                  "to this database, then save "
                                  "again."}), 409
    finally:
        conn.close()
    return jsonify({"ok": True, "saved": len(events),
                    "week_start": str(week)})


def _crossboard_duplicates(v):
    try:
        return q("""
            WITH mine AS (
              SELECT task_id, title FROM tasks_current
              WHERE user_ref = %s AND merged_into IS NULL
                AND status NOT IN ('completed','cancelled')),
            theirs AS (
              SELECT t.task_id, t.title, t.user_ref, t.status,
                     t.created_at
              FROM visible_tasks(%s) vt
              JOIN tasks_current t USING (task_id)
              WHERE t.user_ref <> %s AND t.merged_into IS NULL
                AND t.status NOT IN ('completed','cancelled'))
            SELECT m.task_id, m.title,
                   th.task_id AS other_task_id, th.title AS other_title,
                   th.user_ref AS other_owner, th.status AS other_status,
                   th.created_at AS other_since
            FROM mine m
            JOIN theirs th
              ON lower(regexp_replace(m.title, '\\s+', ' ', 'g'))
               = lower(regexp_replace(th.title, '\\s+', ' ', 'g'))
              OR EXISTS (
                SELECT 1 FROM task_aliases a1
                JOIN task_aliases a2
                  ON lower(regexp_replace(a1.alias, '\\s+', ' ', 'g'))
                   = lower(regexp_replace(a2.alias, '\\s+', ' ', 'g'))
                WHERE a1.task_id = m.task_id
                  AND a2.task_id = th.task_id)
            WHERE NOT EXISTS (
              SELECT 1 FROM crossboard_rulings r
              WHERE r.task_a = least(m.task_id, th.task_id)
                AND r.task_b = greatest(m.task_id, th.task_id))
            ORDER BY th.created_at""", (v, v, v))
    except psycopg2.errors.UndefinedTable:
        return []          # ledger predates migrate_team.sql


@app.route("/api/duplicate", methods=["POST"])
def duplicate_ruling():
    """Rule on a cross-board pair: same work, or genuinely two.

    Either owner may rule, and one ruling settles it for both -- the
    question is symmetric and asking twice would collect a fight. The
    ruling changes NEITHER board: 'same' does not merge across boards
    (a merge erases one owner's history, which is theirs), it records
    the agreed fact and retires the card. Coordination stays human;
    the ledger just stops pretending not to notice.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    ruling = (body.get("ruling") or "").strip()
    if ruling not in ("same", "distinct"):
        return jsonify({"error": "ruling must be same or distinct"}), 400
    a = (body.get("task_id") or "").strip()
    b = (body.get("other_task_id") or "").strip()
    if not a or not b or a == b:
        return jsonify({"error": "two distinct task ids required"}), 400
    rows = q("SELECT task_id, user_ref FROM tasks_current "
             "WHERE task_id = ANY(%s)", ([a, b],))
    owners = {r["task_id"]: r["user_ref"] for r in rows}
    if len(owners) != 2:
        return jsonify({"error": "no such pair"}), 404
    if v not in owners.values():
        return jsonify({"error": "neither task is yours"}), 403
    conn = _write_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO crossboard_rulings (task_a, task_b, ruling,
                  ruled_by)
                VALUES (least(%s,%s), greatest(%s,%s), %s, %s)""",
                (a, b, a, b, ruling, v))
        conn.commit()
    except psycopg2.errors.UndefinedTable:
        return jsonify({"error": "this ledger predates the team "
                                 "migration",
                        "detail": "Apply storage/migrate_team.sql."}), 409
    finally:
        conn.close()
    return jsonify({"ok": True, "ruling": ruling})


# --------------------------------------------------------- receipt pages

def _pretty_source(ref):
    """source_refs are ids; people read words. 'teams-2026-09-10-sprint-
    planning-p2' -> 'sprint planning (2026-09-10)'; 'copilot-claims-...'
    says what it is. The raw ref stays available everywhere -- this is a
    reading aid, not a replacement."""
    if not ref:
        return "(unrecorded)"
    m = re.match(r"(?:teams|meeting|copilot)-(\d{4}-\d{2}-\d{2})-(.+?)"
                 r"(?:-p\d+)?$", ref)
    if m:
        return "{0} ({1})".format(m.group(2).replace("-", " "),
                                  m.group(1))
    m = re.match(r"copilot-claims-(\d{4}-\d{2}-\d{2})-(.+)$", ref)
    if m:
        return "Copilot export of {0}".format(m.group(1))
    if ref.startswith("note-"):
        return "a note typed directly"
    return ref


RECEIPT_CSS = """
body{font:14px/1.55 -apple-system,'Segoe UI',sans-serif;color:#1c2733;
  max-width:760px;margin:2rem auto;padding:0 1rem}
h1{font-size:1.3rem;margin-bottom:.2rem} .sub{color:#5a6b7c;font-size:13px}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;
  font-size:11.5px;border:1.5px solid #c6ccd4;margin-left:.4rem}
.born{background:#f4f7fa;border:1px solid #dfe5ec;border-radius:8px;
  padding:12px 15px;margin:1.1rem 0}
.ev{border-left:3px solid #dfe5ec;margin:.7rem 0;padding:.1rem 0 .1rem 13px}
.ev .w{font-weight:600} .ev .who{color:#5a6b7c;font-size:12.5px}
.ev .prov{font-size:12.5px;color:#3b4a5a;margin-top:2px}
.hash{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#8a97a5}
.foot{margin-top:1.6rem;padding-top:.8rem;border-top:1px solid #dfe5ec;
  color:#5a6b7c;font-size:12px}
@media print{.noprint{display:none}}
"""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


@app.route("/receipt/<task_id>")
def receipt(task_id):
    """The said->tracked->done chain as one page you can paste a link
    to. 'Where did this requirement come from?' answered with a URL
    instead of an argument.

    Identity comes from ?as= (or the header), same trust model as the
    picker this whole demo runs on, and the page SAYS whose view it is
    -- a receipt that hid its viewpoint would undermine the thing it
    exists to prove. Access is task_level: full trace for the owner and
    full_trace grantees, the redacted skeleton at progress, and a
    status_only edge gets title and status, nothing more. Evidence
    stays pointers-and-hashes, never message text: the page proves the
    chain exists and where it anchors, it does not republish anyone's
    words.
    """
    v = (request.args.get("as") or "").strip() or viewer()
    if not v:
        return ("<p>Say who is looking: add ?as=your-ref to the "
                "link.</p>", 401)
    level = task_level(v, task_id)
    if level is None:
        return ("<p>This task is not shared with {0}.</p>".format(
            _esc(v)), 403)
    t = q("SELECT * FROM tasks_current WHERE task_id = %s", (task_id,))
    if not t:
        return ("<p>No such task.</p>", 404)
    t = t[0]
    out = ["<style>{0}</style>".format(RECEIPT_CSS)]
    out.append("<h1>{0}<span class='badge'>{1}</span></h1>".format(
        _esc(t["title"]), _esc(t["status"])))
    out.append("<div class='sub'>owner {0} \u00b7 viewed as {1} "
               "({2} access) \u00b7 {3}</div>".format(
                   _esc(t["owner_ref"] or t["user_ref"]), _esc(v),
                   _esc(level), _esc(task_id)))
    if level == "status_only":
        out.append("<p class='born'>Visible to {0} only because it "
                   "blocks their work: current status, nothing "
                   "more.</p>".format(_esc(v)))
        return "\n".join(out)

    events = q("""
        SELECT e.event_id, e.occurred_at, e.event_type, e.actor_type,
               e.payload, d.decision, d.decided_by,
               p.rationale, p.confidence,
               s.title AS said, s.author_ref AS said_by,
               ev.resource_id, ev.content_hash, b.source_ref
        FROM task_events e
        LEFT JOIN decisions d ON d.decision_id = e.decision_id
        LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
        LEFT JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
        LEFT JOIN signals s ON s.signal_id = ps.signal_id
        LEFT JOIN evidence_refs ev ON ev.signal_id = s.signal_id
                                  AND ev.ordinal = 1
        LEFT JOIN extraction_batches b ON b.batch_id = s.batch_id
        WHERE e.task_id = %s
        ORDER BY e.event_id""", (task_id,))
    seen = set()
    full = (level == "full_trace")
    for e in events:
        if e["event_id"] in seen:
            continue          # one row per event; extra signals collapse
        seen.add(e["event_id"])
        heard = (e["payload"] or {}).get("heard_in")
        if (not heard and e["event_type"] == "created"
                and e["actor_type"] == "manual_edit"):
            rescue = q("""
                SELECT b.source_ref FROM task_aliases a
                JOIN signals s ON s.signal_id = a.source_signal_id
                JOIN extraction_batches b USING (batch_id)
                WHERE a.task_id = %s
                ORDER BY a.added_at LIMIT 1""", (task_id,))
            heard = rescue[0]["source_ref"] if rescue else None
        who = ("accepted by " + _esc(e["decided_by"])
               if e["actor_type"] == "user_decision" and full
               and e["decided_by"]
               else "a person decided" if e["actor_type"]
                   == "user_decision"
               else ("accepted by a person, from a claim heard in "
                     + _esc(_pretty_source(heard)))
                   if e["actor_type"] == "manual_edit" and heard
               else "typed by hand" if e["actor_type"] == "manual_edit"
               else "auto-applied, unconfirmed"
                   if e["actor_type"] == "auto_applied"
               else "system")
        out.append("<div class='ev'><span class='w'>{0}</span> "
                   "<span class='who'>\u00b7 {1} \u00b7 {2}</span>"
                   .format(_esc(e["event_type"].replace("_", " ")),
                           str(e["occurred_at"])[:16], who))
        if full and e["said"]:
            out.append("<div class='prov'>from the claim "
                       "\u201c{0}\u201d{1}{2}</div>"
                       "<div class='hash'>evidence {3} \u00b7 "
                       "hash {4}</div>".format(
                           _esc(e["said"]),
                           " said by " + _esc(e["said_by"])
                           if e["said_by"] else "",
                           " in " + _esc(_pretty_source(
                               e["source_ref"]))
                           if e["source_ref"] else "",
                           _esc(e["resource_id"] or "\u2014"),
                           _esc((e["content_hash"] or "")[:16])))
        if full and e["rationale"]:
            out.append("<div class='prov'>rationale: {0}</div>".format(
                _esc(e["rationale"])))
        out.append("</div>")
    out.append("<div class='foot'>Generated {0}. This record is "
               "append-only: nothing above can be edited, only added "
               "to. Evidence is cited by pointer and content hash "
               "\u2014 the page proves where each fact anchors "
               "without republishing anyone\u2019s words.</div>"
               .format(datetime.date.today().isoformat()))
    return "\n".join(out)


@app.route("/api/scorecard")
def scorecard():
    """The system keeping score on ITSELF, for this viewer -- the
    founding promise, finally read back. Composed sentences comparing
    the last 30 days with the 30 before: your acceptance rate, where
    the rejections went, what applied itself and how often you put it
    back. Never a scorecard of people: every number is about claims
    the system made and what one person ruled about them.
    """
    v = viewer()
    if not v:
        return _need_viewer()

    def window(a, b):
        rows = q("""
            SELECT d.decision, d.reject_reason, count(*) AS n
            FROM decisions d
            JOIN proposals p USING (proposal_id)
            JOIN review_packets rp ON rp.packet_id = p.packet_id
            WHERE rp.user_ref = %s AND d.decision <> 'defer'
              AND d.decided_at >  now() - make_interval(days => %s)
              AND d.decided_at <= now() - make_interval(days => %s)
            GROUP BY 1, 2""", (v, a, b))
        acc = sum(r["n"] for r in rows
                  if r["decision"] in ("accept", "accept_edited"))
        rej = {r["reject_reason"]: r["n"] for r in rows
               if r["decision"] == "reject" and r["reject_reason"]}
        return acc, rej

    acc_now, rej_now = window(30, 0)
    acc_prev, rej_prev = window(60, 30)
    tot_now = acc_now + sum(rej_now.values())
    tot_prev = acc_prev + sum(rej_prev.values())
    lines = []
    if tot_now or tot_prev:
        lines.append({
            "line": "You ruled on {0} claim(s) this month and accepted "
                    "{1}%; last month it was {2} at {3}%.".format(
                        tot_now,
                        round(100 * acc_now / tot_now) if tot_now else 0,
                        tot_prev,
                        round(100 * acc_prev / tot_prev)
                        if tot_prev else 0),
            "kind": "volume"})
    for reason in sorted(set(rej_now) | set(rej_prev)):
        a, b = rej_prev.get(reason, 0), rej_now.get(reason, 0)
        if a == b == 0:
            continue
        lines.append({
            "line": "{0}: {1} \u2192 {2}{3}".format(
                reason.replace("_", " "), a, b,
                " \u2014 improving" if b < a else
                " \u2014 getting worse" if b > a else ""),
            "kind": "better" if b < a else "worse" if b > a else "flat"})
    auto = q("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE EXISTS (
                 SELECT 1 FROM task_events u
                 WHERE u.task_id = e.task_id
                   AND u.actor_type = 'manual_edit'
                   AND u.payload->>'undoes_event_id'
                       = e.event_id::text)) AS undone
        FROM task_events e
        WHERE e.user_ref = %s AND e.actor_type = 'auto_applied'
          AND e.occurred_at > now() - interval '30 days'""", (v,))
    if auto and auto[0]["n"]:
        lines.append({
            "line": "{0} change(s) applied themselves; you put {1} "
                    "back. {2}".format(
                        auto[0]["n"], auto[0]["undone"],
                        "The gate is earning its keep."
                        if auto[0]["undone"] == 0 else
                        "Every put-back tightens the gate\u2019s "
                        "case for being narrower."),
            "kind": "flat" if auto[0]["undone"] else "better"})
    return jsonify({
        "lines": lines,
        "note": "Every number here is about claims the SYSTEM made and "
                "what you ruled -- there is no scorecard of people, "
                "and there never will be.",
    })


# ------------------------------------------------------- friction killers

@app.route("/api/standup")
def standup():
    """SELF ONLY: your standup, drafted from the board, every line with
    its receipt.

    The inverse of everything else here: instead of reading meetings to
    update the board, read the board to write the meeting. Zero risk by
    construction -- it only reads CONFIRMED state, each line cites the
    event behind it, and the person edits before speaking. This is also
    the feature that makes people want their board current, which feeds
    every other part of the system.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    try:
        days = max(1, min(14, int(request.args.get("days", "1"))))
    except ValueError:
        days = 1
    rows = q("""
        SELECT e.task_id, t.title, e.event_type, e.actor_type,
               e.occurred_at, e.payload,
               src.source_ref
        FROM task_events e
        JOIN tasks_current t USING (task_id)
        LEFT JOIN LATERAL (
          SELECT b.source_ref
          FROM decisions d
          JOIN proposals pr ON pr.proposal_id = d.proposal_id
          JOIN proposal_signals ps ON ps.proposal_id = pr.proposal_id
          JOIN signals s ON s.signal_id = ps.signal_id
          JOIN extraction_batches b ON b.batch_id = s.batch_id
          WHERE d.decision_id = e.decision_id
          LIMIT 1) src ON true
        WHERE t.user_ref = %s AND t.merged_into IS NULL
          AND e.occurred_at > now() - make_interval(days => %s)
          AND e.event_type IN ('created','status_changed','completed',
                               'cancelled','blocker_added',
                               'blocker_resolved','deadline_changed')
        ORDER BY e.occurred_at""", (v, days))
    done, moving, blocked, unblocked, new = [], [], [], [], []
    for r in rows:
        when = str(r["occurred_at"])[:10]
        how = ("accepted by you" if r["actor_type"] == "user_decision"
               else "typed by you" if r["actor_type"] == "manual_edit"
               else "auto-applied, unconfirmed")
        cite = " ({0} {1}{2})".format(
            how, when,
            " \u00b7 from " + r["source_ref"] if r["source_ref"] else "")
        et = r["event_type"]
        if et == "completed":
            done.append("Finished: " + r["title"] + cite)
        elif et == "created":
            new.append("Took on: " + r["title"] + cite)
        elif et == "blocker_added":
            d = (r["payload"] or {}).get("description")
            blocked.append("Now blocked: " + r["title"]
                           + (" \u2014 " + d if d else "") + cite)
        elif et == "blocker_resolved":
            unblocked.append("Unblocked: " + r["title"] + cite)
        elif et == "status_changed":
            to = (r["payload"] or {}).get("to_status")
            if to == "in_progress":
                moving.append("Moving: " + r["title"] + cite)
    still = q("""
        SELECT t.title, b.description FROM task_blockers b
        JOIN tasks_current t USING (task_id)
        WHERE t.user_ref = %s AND b.resolved_event_id IS NULL
          AND t.status NOT IN ('completed','cancelled')
          AND t.merged_into IS NULL""", (v,))
    lines = []
    for head, items in (("Done", done), ("In motion", moving + new),
                        ("Blocked", blocked
                         + ["Still blocked: " + s["title"]
                            + (" \u2014 " + s["description"]
                               if s["description"] else "")
                            for s in still]),
                        ("Unblocked", unblocked)):
        if items:
            lines.append(head + ":")
            lines.extend("  \u2022 " + i for i in items)
    text = "\n".join(lines) if lines else (
        "Nothing recorded in the last {0} day(s). Either a quiet "
        "stretch, or the board is behind -- the review popup knows "
        "which.".format(days))
    return jsonify({"days": days, "text": text,
                    "note": "Drafted from confirmed board events only; "
                            "every line carries how and when it was "
                            "recorded. Edit before you say it -- it is "
                            "a draft, not a report card."})


def _reconcile_now(v):
    """One person's pool through the full reconciler, synchronously.

    Offline embedder and no judge, deliberately: deterministic, fast,
    zero model dependencies -- a kit machine has no Ollama and must
    not need one to see its own note become a card. Ambiguous matches
    hold, exactly as the no-judge path always has. The packet write
    uses the same storage path as every tick, so nothing about the
    record says this happened in a request instead of a cadence.
    """
    root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    for sub in ("reconciler",):
        d = os.path.join(root, sub)
        if d not in sys.path:
            sys.path.insert(0, d)
    import board_io_pg
    import embedders
    import reconciler as rec_mod
    dsn = profile_dsn()
    signals = board_io_pg.load_signals(dsn, v)
    if not signals:
        return
    tasks, version = board_io_pg.load_board(dsn, v)
    all_tasks = board_io_pg.load_all_boards(dsn)
    candidates = board_io_pg.load_candidates(
        dsn, v, window_days=(rec_mod.load_policy()
                             .get("candidate_promotion")
                             or {}).get("window_days"))
    run_id = "note-{0}".format(
        datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    rec = rec_mod.Reconciler(embed=embedders.TokenEmbedder(),
                             judge_llm=None)
    packet, report = rec.reconcile(v, signals, tasks, version, run_id,
                                   all_tasks=all_tasks,
                                   candidates=candidates)
    board_io_pg.write_packet(dsn, packet, report["consumed"],
                             report["annotations"], run_id,
                             report=report)


@app.route("/api/note", methods=["POST"])
def quick_note():
    """One typed line -> the same pool, gates and review as a meeting.

    Extraction is skipped DELIBERATELY: there is no transcript to
    comprehend. The typed sentence is the claim, its author is the
    speaker, its confidence is high because the owner said so about
    their own work -- and everything downstream is unchanged: the
    not_work gate can still bin it, a mention still needs the register,
    and nothing lands on the board except through the review the person
    answers themselves.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    kind = (body.get("kind") or "work").strip()
    if not text or len(text) > 300:
        return jsonify({"error": "one line, up to 300 characters"}), 400
    stype = {"work": "proposal", "update": "status_update",
             "blocker": "blocker"}.get(kind)
    if not stype:
        return jsonify({"error": "kind must be work, update or "
                                 "blocker"}), 400
    now = datetime.datetime.now().replace(microsecond=0)
    import hashlib as _h
    rid = "note-{0}-{1}".format(v, now.strftime("%Y%m%dT%H%M%S"))
    batch_id = _h.sha256(("manual-note\x1f" + rid).encode()).hexdigest()[:32]
    signal_id = _h.sha256((batch_id + "\x1f" + text).encode()) \
        .hexdigest()[:32]
    raw = {"signal_id": signal_id, "type": stype, "title": text[:120],
           "description": (text if len(text) > 120 else None),
           "author_ref": v, "confidence": 0.9,
           "owner_candidates": [{"person_ref": v, "confidence": 0.9,
                                 "basis": "self_reference"}],
           "evidence": [{"source_type": "manual_note",
                         "resource_id": rid, "span": None,
                         "content_hash": _h.sha256(
                             text.encode()).hexdigest()}]}
    if stype == "blocker":
        raw["blocker"] = {"description": text[:300],
                          "blocking_party_ref": None}
    conn = _write_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO extraction_batches (batch_id, extracted_at,
                  extractor_model, prompt_version, source_type,
                  source_ref, window_start, window_end)
                VALUES (%s, now(), 'human:typed', 'note-v1',
                        'manual_note', %s, now(), now())
                ON CONFLICT (batch_id) DO NOTHING""", (batch_id, rid))
            cur.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type,
                  title, description, confidence, blocker_desc,
                  author_ref, owner_candidates, raw)
                VALUES (%s,%s,%s,%s,%s,0.9,%s,%s,%s,%s)
                ON CONFLICT (signal_id) DO NOTHING""",
                (signal_id, batch_id, stype, text[:120],
                 raw.get("description"),
                 text[:300] if stype == "blocker" else None,
                 v, json.dumps(raw["owner_candidates"]),
                 json.dumps(raw)))
            cur.execute("""
                INSERT INTO evidence_refs (signal_id, ordinal,
                  source_type, resource_id, content_hash)
                VALUES (%s, 1, 'manual_note', %s, %s)
                ON CONFLICT DO NOTHING""",
                (signal_id, rid, raw["evidence"][0]["content_hash"]))
            cur.execute("""
                INSERT INTO signal_pool_events (signal_id, user_ref,
                  event, run_id)
                VALUES (%s, %s, 'pooled', 'quick-note')""",
                (signal_id, v))
        conn.commit()
    except psycopg2.errors.CheckViolation:
        return jsonify({"error": "this ledger predates manual notes",
                        "detail": "Apply storage/migrate_manual_note"
                                  ".sql, then note again."}), 409
    finally:
        conn.close()
    # INSTANT LOOP. "It becomes a card at the next tick" was written
    # for the machine that runs ticks; a kit never does, so a captured
    # note vanished into the pool forever and the first pilot reviewer
    # watched his own words disappear -- "I don't know where it's
    # going" -- which is the exact feeling this system exists to
    # abolish. The note is now reconciled ON THE SPOT: offline
    # embedder, no judge (ambiguity holds, as ever), same gates, and
    # the card is in the review popup before the response lands. If
    # the inline pass fails for any reason the note is still safely
    # pooled and the old message tells the truth for that case.
    try:
        _reconcile_now(v)
        return jsonify({"ok": True, "reconciled": True,
                        "detail": "Captured, typed by you, through the "
                                  "same gates as everything else -- it "
                                  "is in your review now."})
    except Exception as exc:                            # noqa: BLE001
        print("instant reconcile failed: %r" % (exc,), file=sys.stderr)
        return jsonify({"ok": True, "reconciled": False,
                        "detail": "Captured and safely pooled; it "
                                  "becomes a card at the next reconcile "
                                  "tick."})


@app.route("/api/radar")
def radar():
    """SELF ONLY: deadlines ranked by how much they should worry you.

    Three facts already stored, one glance: overdue; due within 7 days
    AND at risk (blocked, or nothing has moved in a week); merely due.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify({"items": q("""
        SELECT t.task_id, t.title, t.deadline, t.status,
               (now()::date - le.last_moved::date) AS quiet_days,
               CASE
                 WHEN t.deadline < CURRENT_DATE THEN 'overdue'
                 WHEN t.deadline <= CURRENT_DATE + 7
                      AND (t.status = 'blocked'
                           OR le.last_moved < now() - interval '7 days')
                   THEN 'at_risk'
                 WHEN t.deadline <= CURRENT_DATE + 7 THEN 'due_soon'
               END AS bucket
        FROM tasks_current t
        JOIN LATERAL (SELECT max(occurred_at) AS last_moved
                      FROM task_events e
                      WHERE e.task_id = t.task_id) le ON true
        WHERE t.user_ref = %s AND t.merged_into IS NULL
          AND t.status NOT IN ('completed','cancelled')
          AND t.deadline IS NOT NULL
          AND t.deadline <= CURRENT_DATE + 7
        ORDER BY t.deadline""", (v,))})


@app.route("/api/teamweek")
def teamweek():
    """The consented team view: dependency edges the viewer can see
    both ends of, and the week sheets colleagues chose to share.

    Nothing here is discoverable that was not either granted (workload
    sheets, board visibility) or already a cross-board fact (an edge
    both sides can see). No hours appear without a 'workload' grant --
    a shared board must not quietly imply shared hours, because hours
    are the most scorecard-shaped data this system holds.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    deps = q("""
        SELECT wt.user_ref AS waiting_owner, wt.title AS waiting_title,
               bt.user_ref AS on_owner, bt.title AS on_title,
               b.description
        FROM task_blockers b
        JOIN tasks_current wt ON wt.task_id = b.task_id
        JOIN tasks_current bt ON bt.task_id = b.blocking_task_id
        WHERE b.resolved_event_id IS NULL
          AND wt.user_ref <> bt.user_ref
          AND wt.merged_into IS NULL
          AND wt.status NOT IN ('completed','cancelled')
          AND wt.task_id IN (SELECT task_id FROM visible_tasks(%s))
          AND bt.task_id IN (SELECT task_id FROM visible_tasks(%s))
        ORDER BY wt.user_ref""", (v, v))
    shared = []
    try:
        grantors = q("""
            SELECT DISTINCT subject_id FROM v_current_grants
            WHERE grantee_ref = %s AND subject_type = 'user'
              AND level = 'workload'""", (v,))
    except psycopg2.errors.UndefinedTable:
        grantors = []
    week = _monday()
    for g in grantors:
        person = g["subject_id"]
        subs = _workload_subjects(person)
        stated = _stated(person, week) or {}
        rows = []
        for s in subs:
            planned = stated.get((s["subject"], "planned"))
            prio = stated.get((s["subject"], "priority"))
            rows.append({
                "label": s["label"],
                "blocker": s.get("blocker"),
                "planned": (float(planned["hours"]) if planned else None),
                "priority": (int(prio["priority"]) if prio else None),
            })
        cap = (stated.get(("_capacity", "capacity")) or {}).get("hours")
        shared.append({"person": person,
                       "capacity": float(cap) if cap is not None else None,
                       "week_start": str(week), "rows": rows})
    return jsonify({"dependencies": deps, "shared": shared,
                    "note": "Only what was granted: sheets appear here "
                            "because their owners shared them "
                            "(level=workload), and hours never ride "
                            "along with a board grant."})


@app.route("/api/workload/share", methods=["POST"])
def workload_share():
    """Share MY week sheet with a colleague. grant_access enforces that
    only I may share my own; the level is 'workload' and nothing else,
    so this button cannot be talked into sharing a board."""
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip()
    if not to or to == v:
        return jsonify({"error": "say who, and not yourself"}), 400
    known = q("""
        SELECT 1 FROM tasks_current WHERE user_ref = %s
        UNION SELECT 1 FROM signals WHERE author_ref = %s LIMIT 1""",
        (to, to))
    if not known:
        return jsonify({"error": "the ledger does not know that person"
                        }), 400
    conn = _write_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT grant_access(%s,%s,'user',%s,'workload',"
                        "%s,NULL)",
                        (v, to, v, "week sheet, shared from the "
                                   "dashboard"))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "to": to})


@app.route("/api/unblocked", methods=["POST"])
def unblocked_answer():
    """Answer 'your blocker moved': moving again, or still blocked.

    SELF ONLY and human-typed, like every board write from a browser.
    'moving' resolves the blocker through the same blocker_resolved
    event every other path uses -- the projection recomputes status
    itself, so a task with a second open blocker stays blocked, which a
    hand-set status would have gotten wrong. 'still' records a
    status_changed blocked->blocked: a real event, typed by the person,
    which is both the honest answer and what retires the card.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    answer = (body.get("answer") or "").strip()
    if answer not in ("moving", "still"):
        return jsonify({"error": "answer must be moving or still"}), 400
    try:
        blocker_id = int(body.get("blocker_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "blocker_id required"}), 400
    conn = _write_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT b.task_id, t.user_ref, t.status
                FROM task_blockers b
                JOIN tasks_current t ON t.task_id = b.task_id
                WHERE b.blocker_id = %s
                  AND b.resolved_event_id IS NULL""", (blocker_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "no such open blocker"}), 404
            if row["user_ref"] != v:
                return jsonify({"error": "that task is not yours"}), 403
            if answer == "moving":
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'blocker_resolved',"
                    "'manual_edit',NULL,%s::jsonb)",
                    (row["task_id"], v,
                     json.dumps({"blocker_id": blocker_id})))
            else:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'status_changed',"
                    "'manual_edit',NULL,%s::jsonb)",
                    (row["task_id"], v,
                     json.dumps({"from_status": row["status"],
                                 "to_status": "blocked"})))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "answer": answer})


@app.route("/api/nudge", methods=["POST"])
def nudge():
    """Answer 'where did this get to?' on a task that has gone quiet.

    Not a proposal and not a model output: a person typing what is true.
    The schema has carried a place for this from the start --
    actor_type='manual_edit' with a NULL decision_id -- so that "human
    typed it" stays as auditable as "model proposed it", and the
    provenance rail can tell the two apart forever.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if not _writable():
        return _refuse_write()
    body = request.get_json(silent=True) or {}
    task_id = (body.get("task_id") or "").strip()
    status = (body.get("status") or "").strip()
    # 'cancelled' joined the list for the pilot's commonest true
    # answer about old work: "this was real, and it is not relevant
    # anymore." Without it the only exits were pretending it was done
    # or leaving it to haunt the quiet list forever -- the first
    # reviewer met tasks from two months ago and had no honest button.
    if status not in ("in_progress", "blocked", "completed",
                      "cancelled"):
        return jsonify({"error": "status must be in_progress, blocked, "
                                 "completed or cancelled"}), 400
    note = (body.get("note") or "").strip()[:200] or None

    conn = _write_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_ref, status FROM tasks_current "
                        "WHERE task_id = %s AND merged_into IS NULL",
                        (task_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "no such task"}), 404
            if row[0] != v:
                return jsonify({"error": "not your task"}), 403
            event = ("completed" if status == "completed"
                     else "cancelled" if status == "cancelled"
                     else "blocker_added" if status == "blocked"
                     else "status_changed")
            # Payload keys are apply_task_event's contract, not this
            # endpoint's preference: status_changed reads to_status, and
            # blocker_added opens a row in task_blockers from description.
            if status == "blocked":
                payload = {"description": note or "raised from the nudge, "
                                                  "reason not given",
                           "blocking_party": None,
                           "source": "dashboard nudge"}
            else:
                payload = {"to_status": status, "note": note,
                           "source": "dashboard nudge"}
            cur.execute("SELECT apply_task_event(%s,%s,%s,'manual_edit',"
                        "NULL,%s::jsonb)",
                        (task_id, v, event, json.dumps(payload)))
        conn.commit()
        return jsonify({"ok": True, "task_id": task_id, "status": status})
    finally:
        conn.close()


@app.route("/api/meetings")
def meetings_v2():
    """Follow-through per conversation, summarised from the same per-claim
    outcomes the detail view shows -- so the summary and the drill-down can
    never disagree."""
    v = viewer()
    if not v:
        return _need_viewer()
    out = {}
    for r in _claim_rows(v):
        # A typed note is not a meeting. Each one is its own batch, so
        # left in, a person who uses quick capture buries their actual
        # conversations under one-line "meetings" named note-<ts> --
        # the list capped out at 25 of them in testing. A note's
        # follow-through already lives where notes live: the review
        # popup and the standup draft.
        if r["source_type"] == "manual_note":
            continue
        m = out.setdefault(r["source_ref"], {
            "source_ref": r["source_ref"], "source_type": r["source_type"],
            "met_at": r["window_start"], "windows": set(), "claims": 0,
            "done": 0, "on the board": 0, "waiting": 0, "rejected": 0,
            "other": 0})
        m["windows"].add(r["batch_id"])
        m["claims"] += 1
        if r["window_start"] < m["met_at"]:
            m["met_at"] = r["window_start"]
        if r["outcome"] in ("done", "on the board", "waiting", "rejected"):
            m[r["outcome"]] += 1
        else:
            m["other"] += 1
    meetings = []
    for m in out.values():
        m["windows"] = len(m["windows"])
        meetings.append(m)
    meetings.sort(key=lambda m: m["met_at"], reverse=True)
    return jsonify(meetings[:25])


@app.route("/api/meeting/<path:source_ref>")
def meeting_detail_v2(source_ref):
    """Every claim one conversation produced, and what became of it."""
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify({"source_ref": source_ref,
                    "claims": _claim_rows(v, source_ref)})


def _outcome(row):
    """One word for where a claim ended up. The order matters: a claim can
    be both proposed and back in the pool (deferred), and the pool is the
    more useful answer because it means the work is still pending."""
    if row["still_pooled"]:
        return "waiting"
    if row["decision"] in ("accept", "accept_edited"):
        if row["task_status"] == "completed":
            return "done"
        if row["task_status"] in ("cancelled",):
            return "cancelled"
        return "on the board"
    if row["decision"] == "reject":
        return "rejected"
    if row["decision"] == "defer":
        return "deferred"
    if row["op"]:
        return "proposed, undecided"
    return "no proposal"


@app.route("/api/timeline")
def timeline():
    """Every item you can see, as a life on a time axis.

    Three counts tell you how much sits at each stage; they cannot show
    movement, which is the thing this ledger is uniquely able to record.
    One row per item — a claim you have heard, a proposal awaiting you, a
    task on a board — running from the moment it was first SAID to where
    it stands now, with a marker at every change along the way.

    Scope is the usual: visible_tasks for board rows (status_only excluded,
    since one blocked-on task is not a life to narrate), plus the viewer's
    own pooled claims, which are self-only everywhere else and stay so
    here. So the chart shows your work and the work of people who have
    shared with you, and nothing else.
    """
    v = viewer()
    if not v:
        return _need_viewer()

    tasks = q("""
        WITH vis AS (SELECT task_id, access_level FROM visible_tasks(%s)
                     WHERE access_level <> 'status_only')
        SELECT t.task_id, t.title, t.user_ref, t.owner_ref, t.status,
               t.created_at, t.deadline, vis.access_level,
               (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
                AND t.status NOT IN ('completed','cancelled')) AS overdue
        FROM vis JOIN tasks_current t USING (task_id)
        WHERE t.merged_into IS NULL
        ORDER BY t.created_at""", (v,))
    ids = [t["task_id"] for t in tasks]

    events, said = {}, {}
    if ids:
        for e in q("""
                SELECT e.task_id, e.event_id, e.event_type, e.occurred_at,
                       e.actor_type
                FROM task_events e WHERE e.task_id = ANY(%s)
                ORDER BY e.task_id, e.board_version""", (ids,)):
            events.setdefault(e["task_id"], []).append(
                {"at": e["occurred_at"], "type": e["event_type"],
                 "actor": e["actor_type"], "event_id": e["event_id"]})
        # When the work was first SAID, not when it was typed in: the
        # extraction window behind whatever decision created the task.
        for r in q("""
                SELECT e.task_id, min(b.window_start) AS said_at
                FROM task_events e
                JOIN decisions d          ON d.decision_id = e.decision_id
                JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
                JOIN signals s            ON s.signal_id = ps.signal_id
                JOIN extraction_batches b ON b.batch_id = s.batch_id
                WHERE e.task_id = ANY(%s)
                GROUP BY e.task_id""", (ids,)):
            said[r["task_id"]] = r["said_at"]

    items = []
    for t in tasks:
        evs = events.get(t["task_id"], [])
        done = [e for e in evs
                if e["type"] in ("completed", "cancelled")]
        items.append({
            "kind": "task", "id": t["task_id"], "title": t["title"],
            "owner": t["owner_ref"] or t["user_ref"],
            "board": t["user_ref"], "mine": t["user_ref"] == v,
            "status": t["status"], "overdue": t["overdue"],
            "deadline": t["deadline"],
            "first_noted": said.get(t["task_id"]) or t["created_at"],
            "said_traced": t["task_id"] in said,
            "ended_at": done[0]["at"] if done else None,
            "stage": ("done" if t["status"] in ("completed", "cancelled")
                      else "board"),
            "events": evs})

    # Claims that never became tasks: they have a beginning and no line
    # yet, which is exactly what the reader should see.
    #
    # Grouped by what was said, not one row per signal. A claim whose
    # target does not exist is held and re-judged on every tick, and the
    # same sentence recurs across meetings, so an ungrouped list showed
    # "get line codes from Hari" three times and "Extraction of line codes"
    # twice on one screen -- which reads as a confused ledger rather than
    # as three people saying the same thing. The count is the interesting
    # part and it was the part being thrown away.
    heard = {}
    for c in q("""
            SELECT u.signal_id, u.title, u.signal_type, u.author_ref,
                   (SELECT min(pe.occurred_at) FROM signal_pool_events pe
                     WHERE pe.signal_id = u.signal_id
                       AND pe.user_ref = u.user_ref) AS pooled_at,
                   (SELECT b.window_start FROM extraction_batches b
                     WHERE b.batch_id = u.batch_id)  AS said_at
            FROM v_unconsumed_signals u
            WHERE u.user_ref = %s""", (v,)):
        key = " ".join((c["title"] or "").lower().split())
        first = c["said_at"] or c["pooled_at"]
        seen = heard.get(key)
        if seen is None:
            heard[key] = {
                "kind": "claim", "id": c["signal_id"], "title": c["title"],
                "owner": c["author_ref"] or v, "board": v, "mine": True,
                "status": c["signal_type"], "overdue": False,
                "deadline": None, "first_noted": first,
                "said_traced": c["said_at"] is not None,
                "ended_at": None, "stage": "heard", "events": [],
                "repeats": 1}
            continue
        seen["repeats"] += 1
        # Keep the earliest, so the row starts when the work was first
        # raised rather than when it was last repeated.
        if first is not None and (seen["first_noted"] is None
                                  or first < seen["first_noted"]):
            seen["first_noted"] = first
            seen["said_traced"] = c["said_at"] is not None
    for it in heard.values():
        if it["repeats"] > 1:
            it["title"] = "{0}  (said {1}x)".format(it["title"],
                                                    it["repeats"])
        items.append(it)

    pend = q("""
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT count(p.proposal_id) - count(d.decision_id) AS n
        FROM review_packets rp
        JOIN latest l USING (packet_id)
        JOIN proposals p USING (packet_id)
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')""", (v,))
    items.sort(key=lambda i: (i["first_noted"] or ""))
    return jsonify({"items": items,
                    "awaiting": (pend[0]["n"] if pend else 0) or 0,
                    "now": datetime.datetime.now(
                        datetime.timezone.utc).isoformat()})


@app.route("/api/metrics")
def metrics():
    """The measures that need this ledger to exist.

    talk-to-tracking latency is the thesis as a number: every other tracker
    starts its clock when someone types a task in; this one knows when the
    work was first SAID, because the batch window is on the record. The
    rest -- what got confirmed, what generated friction, where the queue
    forms -- are event-log aggregates over the same visible-task scope the
    board uses, so a chart can never show more work than the board would.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    vis = ("WITH vis AS (SELECT task_id FROM visible_tasks(%s) "
           "WHERE access_level <> 'status_only')")

    confirmed = q(vis + """
        SELECT to_char(date_trunc('week', e.occurred_at), 'YYYY-MM-DD') AS wk,
               count(*) FILTER (WHERE e.event_type = 'created')   AS created,
               count(*) FILTER (WHERE e.event_type = 'completed') AS completed
        FROM task_events e JOIN vis USING (task_id)
        WHERE e.occurred_at > now() - interval '12 weeks'
        GROUP BY 1 ORDER BY 1""", (v,))

    friction = q(vis + """
        SELECT to_char(date_trunc('week', e.occurred_at), 'YYYY-MM-DD') AS wk,
               count(*) FILTER (WHERE e.event_type = 'blocker_added')
                 AS blocked,
               count(*) FILTER (WHERE e.event_type = 'deadline_changed')
                 AS moved,
               count(*) FILTER (WHERE e.event_type = 'cancelled')
                 AS cancelled
        FROM task_events e JOIN vis USING (task_id)
        WHERE e.occurred_at > now() - interval '12 weeks'
        GROUP BY 1 ORDER BY 1""", (v,))

    # First mention -> confirmed board state. The join walks the whole
    # provenance chain backwards to the extraction window.
    latency = q(vis + """,
        chain AS (
          SELECT e.task_id, e.occurred_at AS confirmed_at,
                 min(b.window_start)      AS said_at
          FROM task_events e
          JOIN vis USING (task_id)
          JOIN decisions d          ON d.decision_id = e.decision_id
          JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
          JOIN signals s            ON s.signal_id = ps.signal_id
          JOIN extraction_batches b ON b.batch_id = s.batch_id
          WHERE e.event_type = 'created'
          GROUP BY e.task_id, e.occurred_at)
        SELECT to_char(date_trunc('week', confirmed_at), 'YYYY-MM-DD') AS wk,
               round((percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(epoch FROM confirmed_at - said_at)
                          / 3600.0))::numeric, 1) AS median_hours,
               count(*) AS n
        FROM chain GROUP BY 1 ORDER BY 1""", (v,))

    queue = q(vis + """
        SELECT COALESCE(bt.user_ref, b.blocking_party, 'unattributed')
                 AS party,
               round(sum(EXTRACT(epoch FROM
                 COALESCE(re.occurred_at, now()) - oe.occurred_at)
                 / 86400.0)::numeric, 1)             AS days,
               count(*)                              AS blockers,
               count(*) FILTER (WHERE b.resolved_event_id IS NULL) AS open
        FROM task_blockers b
        JOIN vis ON vis.task_id = b.task_id
        JOIN task_events oe       ON oe.event_id = b.opened_event_id
        LEFT JOIN task_events re  ON re.event_id = b.resolved_event_id
        LEFT JOIN tasks_current bt ON bt.task_id = b.blocking_task_id
        GROUP BY 1 ORDER BY days DESC LIMIT 8""", (v,))

    tiles = q(vis + """,
        chain AS (
          SELECT e.occurred_at - min(b.window_start) AS lag
          FROM task_events e
          JOIN vis USING (task_id)
          JOIN decisions d          ON d.decision_id = e.decision_id
          JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
          JOIN signals s            ON s.signal_id = ps.signal_id
          JOIN extraction_batches b ON b.batch_id = s.batch_id
          WHERE e.event_type = 'created'
          GROUP BY e.event_id, e.occurred_at)
        SELECT
          (SELECT round((percentile_cont(0.5) WITHIN GROUP (
             ORDER BY EXTRACT(epoch FROM lag) / 3600.0))::numeric, 1)
           FROM chain)                                   AS median_lag_hours,
          (SELECT count(*) FROM chain)                   AS traced_tasks,
          (SELECT count(*) FROM task_events e JOIN vis USING (task_id)
            WHERE e.event_type = 'deadline_changed')     AS deadline_moves,
          (SELECT count(DISTINCT t.task_id) FROM tasks_current t
             JOIN vis USING (task_id) WHERE t.deadline IS NOT NULL)
                                                         AS dated_tasks,
          (SELECT round(COALESCE(sum(EXTRACT(epoch FROM
             now() - oe.occurred_at) / 86400.0), 0)::numeric, 1)
           FROM task_blockers b JOIN vis ON vis.task_id = b.task_id
           JOIN task_events oe ON oe.event_id = b.opened_event_id
           WHERE b.resolved_event_id IS NULL)            AS blocked_days_open,
          (SELECT count(*) FROM v_unconsumed_signals u
            WHERE u.user_ref = %s
              AND u.signal_type IN ('completion_claim','cancellation'))
                                                         AS unconfirmed_closures
        """, (v, v))

    return jsonify({"confirmed": confirmed, "friction": friction,
                    "latency": latency, "queue": queue,
                    "tiles": tiles[0] if tiles else {}})


@app.route("/api/recent")
def recent():
    """What the viewer has personally confirmed lately.

    Every accepted proposal is a decision with their name on it, and the
    ledger is careful to record that -- but nothing showed it back to them.
    Two uses: an accountability record, and the fastest way to notice your
    own rubber-stamping, since a run of accepts on things you did not
    really read looks exactly like what it is. Self-only by construction:
    it filters on decided_by.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT e.event_id, e.task_id, t.title, e.event_type, e.occurred_at,
               d.decision, p.op, p.confidence
        FROM task_events e
        JOIN decisions d      ON d.decision_id = e.decision_id
        LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
        JOIN tasks_current t  ON t.task_id = e.task_id
        WHERE d.decided_by = %s
        ORDER BY e.event_id DESC LIMIT 8""", (v,)))


@app.route("/api/waiting-on/<user_ref>")
def waiting_on(user_ref):
    """The reciprocal of blocked-on: what this person is waiting for, and
    whether it is moving.

    Being blocked on someone's task is the one situation where the model
    hands out visibility automatically -- status_only, resolved inside
    visible_tasks -- so this endpoint reads that level rather than
    inventing its own rule. Same gate as the dependency view: yourself, or
    a full_trace grant on the whole board.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if v != user_ref:
        grants = q("""
            SELECT 1 FROM v_current_grants
            WHERE grantee_ref = %s AND subject_type = 'user'
              AND subject_id = %s AND level = 'full_trace'""", (v, user_ref))
        if not grants:
            return jsonify({"error":
                "the dependency view is owner-only unless the board is "
                "shared at full trace"}), 403
    return jsonify(q("""
        SELECT mine.task_id      AS my_task,
               mine.title        AS my_title,
               b.description     AS blocker_desc,
               b.blocking_party  AS blocking_party,
               blk.task_id       AS blocking_task,
               blk.title         AS blocking_title,
               blk.status        AS blocking_status,
               blk.user_ref      AS blocking_owner,
               vt.access_level   AS blocking_access
        FROM task_blockers b
        JOIN tasks_current mine ON mine.task_id = b.task_id
        LEFT JOIN tasks_current blk ON blk.task_id = b.blocking_task_id
        LEFT JOIN visible_tasks(%s) vt ON vt.task_id = blk.task_id
        WHERE b.resolved_event_id IS NULL AND mine.user_ref = %s
        ORDER BY blk.status NULLS LAST, mine.task_id""", (v, user_ref)))


@app.route("/api/projects")
def projects():
    """Projects the viewer may see: their own, and any granted to them.

    Project scope is the most useful idea in the access model and was
    invisible -- you could be a member of a workstream with no way to see
    that it existed. Counts run through visible_tasks so a project row
    never reveals more work than the grant behind it covers.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT pr.project_id, pr.name, pr.owner_ref, pr.created_at,
               (pr.owner_ref = %s) AS is_owner,
               g.level AS granted_level, g.expires_at,
               count(vt.task_id)                                  AS tasks,
               count(*) FILTER (WHERE t.status = 'blocked')        AS blocked,
               count(DISTINCT t.user_ref)                          AS people
        FROM projects pr
        LEFT JOIN v_current_grants g
               ON g.grantee_ref = %s AND g.subject_type = 'project'
              AND g.subject_id = pr.project_id
        LEFT JOIN tasks_current t
               ON t.project_id = pr.project_id AND t.merged_into IS NULL
        LEFT JOIN visible_tasks(%s) vt ON vt.task_id = t.task_id
              AND vt.access_level <> 'status_only'
        WHERE pr.owner_ref = %s OR g.grantee_ref IS NOT NULL
        GROUP BY pr.project_id, pr.name, pr.owner_ref, pr.created_at,
                 g.level, g.expires_at
        ORDER BY pr.name""", (v, v, v, v)))


@app.route("/api/grants")
def grants_given():
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT grantee_ref, subject_type, subject_id, level, occurred_at,
               expires_at
        FROM v_current_grants WHERE granted_by = %s
        ORDER BY subject_type, subject_id, grantee_ref""", (v,)))


def _startup_banner():
    """Say what mode this is, before anyone squints at the header to work
    it out.

    Every one of these is an environment variable set in some other
    window, and each has been discovered the slow way -- by looking at
    the running app and noticing something absent. A blind comparison
    that quietly is not blind is the worst of them, because the session
    still runs, the answers still come back, and nothing about them is
    usable.
    """
    ledgers = [n for n in ("real", "synthetic", "copilot") if n in PROFILES]
    print("ledgers:  " + ", ".join(ledgers))
    if BLIND and len([n for n in BLIND_ARMS if n in PROFILES]) >= 2:
        print("blind:    ON -- shown as Ledger A / Ledger B, assigned per "
              "person (salt: {0})".format(BLIND_SALT))
    elif BLIND:
        print("blind:    asked for, but OFF -- it needs both ledgers and "
              "only {0} is configured. Set TL_COPILOT_DSN."
              .format(" and ".join(n for n in BLIND_ARMS if n in PROFILES)
                      or "neither"))
    elif "copilot" in PROFILES:
        print("blind:    off -- both ledgers are shown by NAME, which "
              "tells a reviewer which one to trust before they read a "
              "card. Set TL_BLIND=1 for a comparison.")
    print("writable: " + ("real ledger + copilot (TL_HANDS_ON=1)"
                          if HANDS_ON else
                          "synthetic only -- set TL_HANDS_ON=1 to let "
                          "people answer their own cards"))
    print("feedback: " + SURVEY_DIR)
    print("code:     " + BUILD_ID)
    if KIT and KIT.get("for"):
        print("kit for:  {0} -- answers go back to {1}".format(
            KIT.get("for_name") or KIT["for"],
            KIT.get("return_to") or "whoever built the kit"))
    if os.environ.get("TL_VIEWER_PICKER") != "1":
        print("viewers:  picker off (it enumerates everyone with a task); "
              "type an identity in the header, or set TL_VIEWER_PICKER=1")


if __name__ == "__main__":
    _startup_banner()
    app.run(host="127.0.0.1", port=8090)
