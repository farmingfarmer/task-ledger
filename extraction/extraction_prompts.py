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
    key off it.
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

ATTRIBUTION_SPEC = """For owner_candidates, classify HOW you know who owns the work:
- explicit_assignment: someone with apparent authority directed the work to a
  named person ("Priya, can you take the IAM role", "assigning this to Dev").
- self_reference: the speaker claims the work themselves ("I'll handle the
  deploy", "I'm picking up the OCR fix").
- inferred: anything weaker -- the person talked about the work a lot, the
  work is in their area, pronouns you resolved yourself. When in doubt,
  inferred. Never promote inferred to self_reference because it seems likely."""

NEGATIVE_GUIDANCE = """Do NOT emit signals for:
- opinions, preferences, or venting ("this codebase is a mess")
- questions with no commitment attached ("should we upgrade Postgres?")
- hypotheticals and conditionals that were not agreed to ("if we ever get
  budget, we could...")
- work described as finished LONG AGO and merely referenced ("back when I
  built the pipeline...")
- meta-conversation about the meeting itself (scheduling the next call,
  minute-taking)
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

SOURCE_FRAMING = {
    "meeting_transcript": ("The items below are utterances from a meeting "
                           "transcript, in order, with speakers."),
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
{attribution}

WHAT NOT TO EXTRACT
{negative}

OUTPUT
{output}"""


def build_messages(source_type, window_end_date, items):
    """Build the messages array for one extraction call.

    items: list of dicts with keys item_no, author_name, timestamp, text
    window_end_date: 'YYYY-MM-DD' used as the anchor for relative dates.
    """
    system = SYSTEM_PROMPT.format(
        signal_types=SIGNAL_TYPES_SPEC,
        attribution=ATTRIBUTION_SPEC,
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
