"""Extraction prompts: communication window -> task signals.

One system prompt shared across source types, with a per-source framing line.
The model receives the window as NUMBERED ITEMS and must cite item numbers as
evidence for every signal -- the worker converts citations into EvidenceRefs
(resource ids + content hashes), so a signal without citations is dropped
before it ever reaches the store.

Design rules embodied here:
  - Output is a strict JSON array, no prose, no fences. The worker tolerates
    fences anyway, but the prompt does not invite them.
  - The model reports what was SAID, not what should happen to any board.
    Extraction is board-blind by contract; the prompt never mentions tasks
    already existing.
  - Attribution basis is a closed set (explicit_assignment, self_reference,
    inferred) with definitions and examples, because the downstream op gates
    key off it. The fourth answer -- NO owner at all -- is spelled out just
    as explicitly, because a model given three bases and no permission to
    return none will always find someone: measured against planted labels,
    every false attribution was an impersonal sentence ("X is stuck waiting
    on the platform team") pinned on whoever said it. The rule is structural
    (no first person and no named person -> empty), not a confidence
    threshold, since "when in doubt" is what produced the guessing.
  - That rule is right for a room and wrong for a mailbox, so it carries one
    channel exception. An email or document item is written by one person in
    their own voice, and a sender describing work impersonally is reporting
    on work they hold -- measured, treating those as unowned cost the email
    corpus nine points of attribution. Only AUTHORED_SOURCES see the
    exception; every other source type gets prompt text byte-identical to
    what was measured without it, which is what lets those numbers stand.
  - Deadlines carry raw_text + precision always; the resolved date only when
    the model is confident, resolved against the window end date it is given.
  - Negative guidance gets as many words as positive: what is NOT a signal
    (opinions, questions without commitment, hypotheticals, past-completed
    work being reminisced about) is where extractors over-fire.

Python 3.8 compatible. Stdlib only.
"""

SIGNAL_TYPES_SPEC = """- task_mention: a unit of work is referenced without new state information.
- proposal: someone floats work nobody has committed to ("we should probably
  rebuild the intake form"). Commitment language ("I'll", "assigned to",
  "let's have X do it") upgrades it to assignment or task_mention.
- assignment: work is given to or claimed by a specific person.
- status_update: an in-flight state is reported (started, halfway, in review,
  deployed to staging).
- completion_claim: someone states work is DONE. Only use this for finished
  claims, never for progress. Be conservative: "almost done" is a
  status_update.
- deadline_signal: a date or timeframe is attached to work.
- blocker: work is stated to be stuck on something or someone.
- blocker_resolved: a previously stated obstruction is said to be cleared.
- cancellation: work is called off."""

ATTRIBUTION_SPEC = """For owner_candidates, name who would DO the work, and classify how you know:
- explicit_assignment: the work is directed at a named person -- handed over
  ("assigning this to Dev") or addressed to them as a request ("Priya, can
  you take the IAM role?"). The owner is the person ADDRESSED, never the
  speaker who addressed them.
- self_reference: the speaker takes the work on themselves. This is about
  first person, not about signal type -- "I'll handle the deploy", "I'm
  blocked on the migration until access clears", and "finished the OCR fix
  yesterday" are all self_reference, because in each one the person speaking
  is the person doing the work. Transcripts routinely drop the leading "I":
  "will start on the audit tomorrow" and "picking that up after standup" are
  still first person.
- inferred: a named person is tied to the work more weakly -- someone reports
  on a third party ("i think Dev finished the docs refresh"), or a pronoun
  you resolved yourself. The owner is the person NAMED, not the person doing
  the reporting.

LEAVE owner_candidates EMPTY WHEN THE SENTENCE IDENTIFIES NOBODY. An empty
list is a normal, frequent, CORRECT answer -- not a failure to try. Apply it
structurally: if there is no first person AND no person named, there is no
owner to report, however obvious a guess might feel.
- impersonal subjects: "the retention sweep is stuck waiting on the platform
  team", "the migration needs to land before the audit", "that one is no
  longer blocked" -> EMPTY. Work being stuck or due says nothing about who
  holds it.
- collective or nobody in particular: "let's get the intake form scoped",
  "we should prioritise this", "someone should look at it" -> EMPTY.
- Raising a topic is not owning it. The speaker is NOT the owner merely
  because the speaker is the one who brought the work up.
Emitting a wrong owner is worse than emitting none: an unowned signal is
routed by the surrounding system, while a wrong one puts work on the wrong
person's board. Never fill the list just to have it non-empty."""

NEGATIVE_GUIDANCE = """Do NOT emit signals for:
- opinions, preferences, or venting ("this codebase is a mess")
- questions with no commitment attached ("should we upgrade Postgres?")
- hypotheticals and conditionals that were not agreed to ("if we ever get
  budget, we could...")
- work described as finished LONG AGO and merely referenced ("back when I
  built the pipeline...")
- meta-conversation about the meeting itself (scheduling the next call,
  minute-taking)
- ANYTHING THAT HAPPENS DURING THIS MEETING and is finished when it ends.
  Sharing or showing a screen, pulling something up, walking through a
  document, reading a chart aloud, handing over to the next speaker.
  "Let me share my screen and show the spring planning board" is one
  person operating a computer for thirty seconds, not work anybody does
  afterwards. Test it this way: if the sentence describes something the
  speaker is doing AS THEY SPEAK, and nothing remains to be done once
  the call ends, it is not a signal at any confidence.
- pleasantries, greetings, or acknowledgments
- administrative, HR, facilities, or social announcements, EVEN WHEN they
  contain concrete dates. "benefits enrollment closes friday" is not a
  deadline_signal -- there is no work item. A date only matters if it is
  attached to actual work someone will do.

NEGATIONS AND REVERSALS -- read the direction of the statement:
- "we are NOT doing X" / "park X" / "X is descoped" -> this is a
  cancellation signal (or nothing), never a proposal or assignment for X.
- "X is no longer blocked" / "unblocked" -> blocker_resolved or a
  status_update, NEVER a blocker.
- "the deadline on X is dropped/removed" -> NOT a deadline_signal.
  deadline_signal means a date was set or changed TO something. A removed
  deadline with no new date is at most a status_update; if unsure, emit
  nothing for it.

DO emit (commonly missed):
- passing references to known work ("this overlaps with the OCR fix",
  "same area as the drift monitor") -> task_mention. Sightings matter even
  with nothing new to say; report every distinct task referenced.
- hedged secondhand reports ("i think Dev finished the docs refresh?") ->
  emit the completion_claim with LOWER confidence and basis "inferred".
  Hedging affects confidence, not existence. Only suppress when you cannot
  tell which task is meant at all.
One real signal beats three speculative ones, but a hedged sighting of
real work is a real signal."""

OUTPUT_SPEC = """Respond with ONLY a JSON array (no markdown fences, no commentary). Each
element:
{
  "type": "<one of the signal types>",
  "title": "<short imperative, max 120 chars, your words not a quote>",
  "description": "<one or two sentences of context, optional>",
  "speaker": "<display name of who said the key utterance>",
  "owner_candidates": [
    {"name": "<display name>", "basis": "explicit_assignment|self_reference|inferred",
     "confidence": <0..1>}
  ],
  "status_signal": "proposed|in_progress|blocked|completed|cancelled|null",
  "deadline": {"raw_text": "<phrase as spoken>",
               "precision": "exact|week|month|quarter|vague",
               "date": "<YYYY-MM-DD or null if not confidently resolvable>"}
              or null,
  "blocker": {"description": "<what it is stuck on>",
              "blocking_party": "<display name or null>"} or null,
  "confidence": <0..1 that this signal is real and correctly typed>,
  "evidence_items": [<item numbers from the window that support this signal>]
}
Rules:
- evidence_items is REQUIRED and non-empty; cite the minimum items that
  support the signal.
- Resolve relative dates ("Friday", "end of next sprint") against the window
  end date you were given; if you cannot resolve confidently, set date null
  and keep raw_text -- never guess a date.
- Use names exactly as they appear in the items; do not invent people.
- If the window contains no signals, return []."""

# The empty-owner rule is right for a room and wrong for a mailbox.
# "The drift monitor is blocked -- waiting on the cloud team" spoken in a
# standup names nobody: attributing it to whoever said it is what produced
# 15 of 22 real wrong_owner rejections. The same sentence TYPED BY ONE
# PERSON in their own email is that person reporting on their own work, and
# scoring it as unowned costs the email corpus nine points of attribution.
# The difference is not the sentence, it is the channel -- so only the
# channels where a message has a single author in their own voice get this,
# and every other source type sees the structural rule unchanged.
AUTHORED_ATTRIBUTION = """
AUTHORED MESSAGES -- one exception to the empty-owner rule above. Every
item here is written by one person in their own voice rather than spoken
across a room, so a sender reporting on work is normally reporting on work
they hold. When the message does not identify who holds the work, name THE
SENDER, basis "inferred".

Read "does not identify an owner" carefully -- it is not the same as "no
name appears". A person mentioned as something the work WAITS ON is a
dependency, not an owner:
  "the drift monitor is blocked -- waiting on Victor's team for review"
  -> the sender owns the drift monitor; Victor's team is the blocker.
  "the migration still needs to land before the audit"
  -> the sender owns the migration.
Naming a reviewer, a blocking team, or someone whose input is needed
leaves the owner unidentified, and in an authored message that means the
sender.

This does not license guessing anywhere else. If the message hands the
work to someone else, that is explicit_assignment to them; if it reports
what someone else did or will do, the owner is that person; and a sender
asking an open question of the thread ("should we prioritise this?")
proposes work nobody has taken, so it still gets an empty list."""

# Source types whose items are authored messages rather than utterances.
# meeting_recap is deliberately NOT here: it has no sender. It is written
# ABOUT other people by a summariser, so the sender-owns-it rule would
# attribute everybody's work to nobody, or to whoever got summarised first.
AUTHORED_SOURCES = ("email_thread", "document")

# A recap is not a conversation, and the difference is the whole reason it
# gets its own source type. Each item is a subject somebody was reported to
# have discussed -- no utterance, no timestamp, no commitment language. The
# temptation is to read a bullet like "Prompt gallery rollout" as a task
# with an owner and a status; almost all of that would be invented, and the
# invented parts are exactly the ones a person then has to reject.
RECAP_GUIDANCE = """
MEETING RECAP -- read this before extracting anything below.
These items are NOT things people said. They are subjects a summariser
reported each person as having discussed, so treat them as sightings of
work, not as claims about it.

- Emit task_mention, and nothing else. A recap cannot witness a state
  change: it does not record that work started, finished, slipped, got
  blocked or got cancelled, however much a bullet may sound like it.
  "Readiness for rollout" is a subject, not a completion.
- NEVER emit completion_claim, blocker, blocker_resolved, deadline_signal,
  cancellation, or status_update from a recap. If a bullet seems to say
  one of those, it is the summariser's phrasing, not somebody's report.
- The person a bullet sits under discussed it. That is worth recording as
  an owner_candidate with basis "inferred" and modest confidence -- but it
  is a weaker fact than "they said they would do it", and it must never be
  promoted to self_reference or explicit_assignment, neither of which a
  recap can evidence.
- A bullet naming a second person ("handing the template work to Priya")
  still gets basis "inferred" on that person. A summary of an assignment
  is not the assignment.
- Do not merge bullets, and do not invent detail to make one sound like a
  task. Report the subject in the words given."""

RECAP_SOURCES = ("meeting_recap",)

SOURCE_FRAMING = {
    "meeting_transcript": ("The items below are utterances from a meeting "
                           "transcript, in order, with speakers."),
    "meeting_recap": ("The items below are NOT utterances. Each is a "
                      "subject that an automated recap reported a named "
                      "person as having discussed in a meeting. Nobody is "
                      "quoted and no times are recorded."),
    "teams_channel": ("The items below are messages from a Teams channel "
                      "thread, in order, with authors."),
    "teams_chat": ("The items below are messages from a Teams group chat, "
                   "in order, with authors."),
    "email_thread": ("The items below are emails from one thread, oldest "
                     "first, with senders."),
    "document": ("The items below are sections of a shared document, in "
                 "order, with last editors."),
}

SYSTEM_PROMPT = """You extract work signals from workplace communications for a task-tracking
pipeline. You report what was said about work: you never decide what any task
board should do, and you know nothing about any existing tasks.

SIGNAL TYPES
{signal_types}

ATTRIBUTION
{attribution}{channel}

WHAT NOT TO EXTRACT
{negative}

OUTPUT
{output}"""


def build_messages(source_type, window_end_date, items):
    """Build the messages array for one extraction call.

    items: list of dicts with keys item_no, author_name, timestamp, text
    window_end_date: 'YYYY-MM-DD' used as the anchor for relative dates.
    """
    # Empty for every other source type, so their prompt text is
    # byte-identical to what was measured before this exception existed.
    channel = (AUTHORED_ATTRIBUTION
               if source_type in AUTHORED_SOURCES
               else RECAP_GUIDANCE if source_type in RECAP_SOURCES
               else "")
    system = SYSTEM_PROMPT.format(
        signal_types=SIGNAL_TYPES_SPEC,
        attribution=ATTRIBUTION_SPEC,
        channel=channel,
        negative=NEGATIVE_GUIDANCE,
        output=OUTPUT_SPEC,
    )
    framing = SOURCE_FRAMING.get(source_type, SOURCE_FRAMING["teams_chat"])
    lines = [framing,
             "Window end date for resolving relative dates: {0}".format(
                 window_end_date),
             ""]
    for it in items:
        lines.append("[{0}] {1} ({2}): {3}".format(
            it["item_no"], it["author_name"], it["timestamp"], it["text"]))
    lines.append("")
    lines.append("Extract the work signals as a JSON array.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]
