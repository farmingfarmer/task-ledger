"""Judge prompts for the reconciliation matching stage.

Two judges share one output schema:
  1. signal->task judge: does this extracted signal refer to one of these
     candidate tasks? Runs on every signal landing in the judge band.
  2. task<->task merge judge: are these two board tasks the same work item?
     Runs in the periodic merge scan.

Design rules embodied here:
  - Output is strict JSON, no prose, so downstream parsing is a json.loads.
  - The judge must pick a relationship, not just yes/no: same_task,
    subtask_of, related, distinct. subtask_of maps to the add_subtask op;
    related maps to an annotation, never an op.
  - The judge must name the deciding factor. This string is surfaced in
    Proposal.payload.match_note / judge_rationale and is the main thing a
    human sees when deciding whether to trust a merge.
  - Explicit negative guidance: temporal adjacency, shared project names,
    and shared people are NOT identity.

Python 3.8 compatible. No third-party imports.
"""

import json

JUDGE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "confidence", "deciding_factor"],
    "properties": {
        "verdict": {
            "enum": ["same_task", "subtask_of", "related", "distinct"]
        },
        "matched_task_id": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "deciding_factor": {"type": "string", "maxLength": 200},
    },
}

SIGNAL_TASK_JUDGE_SYSTEM = """You are a matching judge inside a task-tracking pipeline. You receive one SIGNAL (a work item mentioned in a recent communication) and up to {top_k} CANDIDATE tasks from an existing task board. Decide whether the signal refers to one of the candidates.

Definitions:
- same_task: the signal and a candidate describe the same unit of deliverable work. The test is identity of the deliverable (what artifact or outcome changes when the work is done), not identity of wording.
- subtask_of: the signal describes work that is strictly a component of a candidate — narrower scope, same deliverable lineage.
- related: same project or theme, but a different deliverable. This is NOT a match.
- distinct: none of the above.

Judging rules:
1. Compare deliverables, not vocabulary. "Migrate the readmission model to Fargate" and "move the hospital model off EC2" are the same task if they change the same deployment.
2. Scope direction matters. If the signal is narrower than a candidate, prefer subtask_of over same_task. If the signal is broader than every candidate, it is distinct (a parent task is never auto-created by matching).
3. The following are NOT evidence of identity, alone or combined: same project name, same people involved, same meeting, adjacent timing, same technology stack. Two tasks in the same sprint about the same service can still be distinct.
4. Status is context, not identity. A completed candidate can still be the correct match (the pipeline handles reopen/conflict logic — do not avoid a match because the candidate is closed).
5. Aliases are prior confirmed references to the candidate. A near-verbatim hit on an alias is strong evidence.
6. If two candidates both plausibly match, pick the one with the more specific deliverable overlap and lower your confidence accordingly.
7. If unsure between same_task and related, choose related. A false merge corrupts task history; a missed merge only delays it.

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"verdict": "same_task|subtask_of|related|distinct", "matched_task_id": "<id or null>", "confidence": <0..1>, "deciding_factor": "<one sentence naming the specific field or phrase that decided it>"}}

matched_task_id must be null when verdict is distinct. confidence reflects your certainty in the verdict, not in the task's importance."""

SIGNAL_TASK_JUDGE_USER = """SIGNAL
type: {signal_type}
title: {signal_title}
description: {signal_description}
speaker/attribution: {signal_attribution}
evidence excerpts:
{evidence_excerpts}

CANDIDATES
{candidates_block}

Which candidate, if any, does the signal refer to?"""

CANDIDATE_BLOCK_TEMPLATE = """[{task_id}] {title}  (status: {status}, owner: {owner}, similarity: {similarity:.2f})
  description: {description}
  aliases: {aliases}
  last activity: {last_activity}"""

MERGE_JUDGE_SYSTEM = """You are a deduplication judge for a task board. You receive two TASKS that an embedding scan flagged as possibly the same work item. Decide their relationship.

Definitions and rules are identical to signal matching: identity means identity of the deliverable. same_task means these two board entries track the same unit of work and should be merged. subtask_of means task B is strictly a component of task A (or vice versa — say which in deciding_factor). related and distinct mean no merge.

Additional rules for merges:
1. Weigh accepted history heavily. Each task's alias list and decision history are human-confirmed. If humans have confirmed different deadlines or different owners on the two tasks, that is strong evidence they are distinct even if titles are near-identical.
2. Sequential phases are not duplicates. "Design X" and "Implement X" share a deliverable lineage but are distinct tasks (or siblings under a parent), never a merge.
3. Recurring work is not a duplicate. "July model retrain" and "August model retrain" are distinct.
4. When verdict is same_task, matched_task_id must be the id of the task that should SURVIVE the merge: the one with the richer confirmed history (more decisions, more aliases), tie-broken by older creation date.
5. If unsure, choose related. Merges are the most trust-expensive operation on the board.

Respond with ONLY a JSON object, no markdown fences:
{{"verdict": "same_task|subtask_of|related|distinct", "matched_task_id": "<surviving id or null>", "confidence": <0..1>, "deciding_factor": "<one sentence>"}}"""

MERGE_JUDGE_USER = """TASK A
{task_a_block}

TASK B
{task_b_block}

Are these the same work item?"""

TASK_BLOCK_TEMPLATE = """[{task_id}] {title}  (status: {status}, owner: {owner}, created: {created})
  description: {description}
  aliases: {aliases}
  confirmed history: {history_summary}
  open blockers: {blockers}
  deadline: {deadline}"""


def build_signal_judge_messages(signal, candidates, top_k=3):
    """Build the messages array for a signal->task judge call.

    signal: dict with keys signal_type, title, description, attribution,
            evidence_excerpts (list of str)
    candidates: list of dicts with keys task_id, title, status, owner,
            similarity, description, aliases (list), last_activity
    Returns list of {"role": ..., "content": ...} dicts.
    """
    excerpts = "\n".join(
        "  - {0}".format(e) for e in signal.get("evidence_excerpts", [])
    ) or "  (none)"
    cand_blocks = "\n\n".join(
        CANDIDATE_BLOCK_TEMPLATE.format(
            task_id=c["task_id"],
            title=c["title"],
            status=c.get("status", "unknown"),
            owner=c.get("owner", "unowned"),
            similarity=c.get("similarity", 0.0),
            description=c.get("description") or "(none)",
            aliases=", ".join(c.get("aliases", [])) or "(none)",
            last_activity=c.get("last_activity", "unknown"),
        )
        for c in candidates[:top_k]
    )
    return [
        {
            "role": "system",
            "content": SIGNAL_TASK_JUDGE_SYSTEM.format(top_k=top_k),
        },
        {
            "role": "user",
            "content": SIGNAL_TASK_JUDGE_USER.format(
                signal_type=signal["signal_type"],
                signal_title=signal["title"],
                signal_description=signal.get("description") or "(none)",
                signal_attribution=signal.get("attribution", "unknown"),
                evidence_excerpts=excerpts,
                candidates_block=cand_blocks,
            ),
        },
    ]


def build_merge_judge_messages(task_a, task_b):
    """Build the messages array for a task<->task merge judge call."""

    def block(t):
        return TASK_BLOCK_TEMPLATE.format(
            task_id=t["task_id"],
            title=t["title"],
            status=t.get("status", "unknown"),
            owner=t.get("owner", "unowned"),
            created=t.get("created", "unknown"),
            description=t.get("description") or "(none)",
            aliases=", ".join(t.get("aliases", [])) or "(none)",
            history_summary=t.get("history_summary", "(none)"),
            blockers=t.get("blockers", "(none)"),
            deadline=t.get("deadline", "(none)"),
        )

    return [
        {"role": "system", "content": MERGE_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": MERGE_JUDGE_USER.format(
                task_a_block=block(task_a), task_b_block=block(task_b)
            ),
        },
    ]


def parse_judge_response(raw_text):
    """Parse and validate a judge response. Returns dict or raises ValueError.

    Deliberately strict: a judge that returns prose or a malformed verdict is
    treated as verdict=distinct by the caller (fail toward no-match), and the
    raw text is logged for prompt regression review.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    obj = json.loads(text)
    verdict = obj.get("verdict")
    if verdict not in ("same_task", "subtask_of", "related", "distinct"):
        raise ValueError("invalid verdict: {0!r}".format(verdict))
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        raise ValueError("invalid confidence: {0!r}".format(conf))
    if verdict == "distinct" and obj.get("matched_task_id") is not None:
        raise ValueError("distinct verdict must carry null matched_task_id")
    if verdict in ("same_task", "subtask_of") and not obj.get("matched_task_id"):
        raise ValueError("{0} verdict requires matched_task_id".format(verdict))
    if not obj.get("deciding_factor"):
        raise ValueError("deciding_factor is required")
    return obj
