"""Adaptive Card layout for the review loop.

Renders a ReviewPacket into an Adaptive Card 1.5 payload using Universal
Actions (Action.Execute), so the same card works in Teams and Outlook and
decisions post back to the bot as invoke activities rather than new messages.

Card anatomy (delivered state):

  header      "Task review - Wed Jul 16" | n proposals | board v41
  [conflicts first, then by descending confidence]
  per proposal:
      op chip + rationale (TextBlock, wrap)
      target task title / new-task title
      facts: change summary, confidence, source phrase (raw_text) when present
      evidence: "View source" Action.OpenUrl per evidence ref (Teams deep link)
      conflict warning container (attention style) when proposal.conflict set
      ActionSet: Accept | Edit (ShowCard, op-specific inputs) | Reject
                 (ShowCard with reason ChoiceSet) | Defer
  footer      Accept all unconflicted | Defer all

Refresh states (Action.Execute verb "card/refresh" + serverside re-render):
  partially_decided -> decided rows collapse to one-line receipts
  closed            -> summary receipt ("6 accepted, 1 rejected, 1 deferred")
  superseded        -> "Superseded by the HH:MM review" stub
  expired           -> "Expired; items will resurface" stub

Verbs posted via Action.Execute data:
  {"verb": "decide", "packet_id", "proposal_id", "decision": "accept"|"defer"}
  {"verb": "decide", ..., "decision": "reject", "reject_reason": <from input>}
  {"verb": "decide", ..., "decision": "accept_edited", "edited_payload": {...}}
  {"verb": "bulk", "packet_id", "action": "accept_unconflicted"|"defer_all"}
  {"verb": "card/refresh", "packet_id"}

Teams cards must stay under 28 KB; MAX_PROPOSALS_PER_CARD caps the render and
overflow goes to a "and N more" continuation row that pages via card refresh.

Python 3.8 compatible. Stdlib only. No emojis.
"""

import json

AC_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
AC_VERSION = "1.5"
MAX_PROPOSALS_PER_CARD = 8
MAX_CARD_BYTES = 28 * 1024

OP_LABELS = {
    "create_task": "New task",
    "add_subtask": "New subtask",
    "update_status": "Status",
    "update_deadline": "Deadline",
    "reassign": "Owner",
    "add_blocker": "Blocker",
    "resolve_blocker": "Blocker cleared",
    "complete_task": "Complete",
    "cancel_task": "Cancel",
    "merge_tasks": "Merge",
}

REJECT_REASON_CHOICES = [
    {"title": "Not actually a task", "value": "not_a_task"},
    {"title": "Duplicate of existing task", "value": "duplicate"},
    {"title": "Matched the wrong task", "value": "wrong_task_match"},
    {"title": "Wrong status", "value": "wrong_status"},
    {"title": "Wrong owner", "value": "wrong_owner"},
    {"title": "Date resolved wrong", "value": "bad_date_resolution"},
    {"title": "Out of date", "value": "stale"},
    {"title": "Other", "value": "other"},
]


def build_review_card(packet, page=0, task_titles=None):
    """Render a ReviewPacket (delivered state) to an Adaptive Card dict.

    task_titles: optional {task_id: title} for display; falls back to ids.
    Proposals are ordered conflicts-first, then by descending confidence,
    so the items needing human judgment are above the fold."""
    task_titles = task_titles or {}
    ordered = sorted(
        packet["proposals"],
        key=lambda p: (0 if p.get("conflict") else 1, -p.get("confidence", 0)),
    )
    start = page * MAX_PROPOSALS_PER_CARD
    visible = ordered[start:start + MAX_PROPOSALS_PER_CARD]
    overflow = len(ordered) - (start + len(visible))

    body = [_header(packet, len(ordered))]
    for prop in visible:
        body.append(_proposal_container(packet, prop, task_titles))
    if overflow > 0:
        body.append(_overflow_row(packet, overflow, page))

    card = {
        "$schema": AC_SCHEMA,
        "type": "AdaptiveCard",
        "version": AC_VERSION,
        "refresh": {
            "action": {
                "type": "Action.Execute",
                "verb": "card/refresh",
                "data": {"packet_id": packet["packet_id"], "page": page},
            },
            "userIds": [packet["user_ref"]],
        },
        "body": body,
        "actions": [
            {
                "type": "Action.Execute",
                "title": "Accept all unconflicted",
                "verb": "bulk",
                "data": {"packet_id": packet["packet_id"],
                         "action": "accept_unconflicted"},
            },
            {
                "type": "Action.Execute",
                "title": "Defer all",
                "verb": "bulk",
                "data": {"packet_id": packet["packet_id"],
                         "action": "defer_all"},
            },
        ],
    }
    _assert_size(card)
    return card


def build_terminal_card(packet, state, summary_line):
    """Stub card for closed / superseded / expired packets."""
    card = {
        "$schema": AC_SCHEMA,
        "type": "AdaptiveCard",
        "version": AC_VERSION,
        "body": [
            {"type": "TextBlock", "text": "Task review",
             "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock", "text": summary_line,
             "wrap": True, "isSubtle": True},
        ],
    }
    if state == "expired":
        card["body"].append({
            "type": "TextBlock", "wrap": True, "isSubtle": True,
            "text": "Unreviewed items were returned to the pool and will "
                    "resurface in your next review.",
        })
    return card


# ------------------------------------------------------------------ sections

def _header(packet, total):
    return {
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": "stretch", "items": [
                {"type": "TextBlock", "text": "Task review",
                 "weight": "Bolder", "size": "Medium"},
                {"type": "TextBlock", "isSubtle": True, "spacing": "None",
                 "text": "{0} proposed change{1} from your recent "
                         "meetings, chats, and email".format(
                             total, "" if total == 1 else "s")},
            ]},
            {"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "isSubtle": True, "size": "Small",
                 "text": "board v{0}".format(packet["board_version"])},
            ]},
        ],
    }


def _proposal_container(packet, prop, task_titles):
    op = prop["op"]
    payload = prop["payload"]
    items = []

    title_col = {
        "type": "Column", "width": "stretch", "items": [
            {"type": "TextBlock", "weight": "Bolder", "wrap": True,
             "text": _proposal_title(prop, task_titles)},
        ],
    }
    chip_col = {
        "type": "Column", "width": "auto", "items": [
            {"type": "TextBlock", "size": "Small", "weight": "Bolder",
             "color": _op_color(op),
             "text": OP_LABELS.get(op, op)},
        ],
    }
    items.append({"type": "ColumnSet", "columns": [chip_col, title_col]})

    items.append({"type": "TextBlock", "wrap": True, "spacing": "Small",
                  "text": prop["rationale"]})

    facts = _facts(prop)
    if facts:
        items.append({"type": "FactSet", "spacing": "Small", "facts": facts})

    if prop.get("conflict"):
        items.append({
            "type": "Container", "style": "attention", "spacing": "Small",
            "items": [{"type": "TextBlock", "wrap": True, "size": "Small",
                       "text": "Conflict: {0}".format(
                           prop["conflict"]["detail"])}],
        })

    ev_actions = _evidence_actions(prop)
    decide_actions = [
        _decide_action(packet, prop, "Accept", "accept"),
        _edit_showcard(packet, prop),
        _reject_showcard(packet, prop),
        _decide_action(packet, prop, "Later", "defer"),
    ]
    items.append({"type": "ActionSet",
                  "actions": ev_actions + [a for a in decide_actions if a]})

    return {"type": "Container", "separator": True, "spacing": "Medium",
            "items": items}


def _proposal_title(prop, task_titles):
    payload = prop["payload"]
    if prop["op"] in ("create_task", "add_subtask"):
        return payload["title"]
    if prop["op"] == "merge_tasks":
        return "Merge {0} duplicate task{1} into one".format(
            len(payload["merged_task_ids"]),
            "" if len(payload["merged_task_ids"]) == 1 else "s")
    tid = prop.get("target_task_id")
    return task_titles.get(tid, tid or "(unknown task)")


def _facts(prop):
    payload = prop["payload"]
    op = prop["op"]
    facts = []
    if op == "update_status":
        facts.append({"title": "Change",
                      "value": "{0} to {1}".format(payload["from_status"],
                                                   payload["to_status"])})
    elif op == "update_deadline":
        frm = payload.get("from_deadline") or "none"
        facts.append({"title": "Deadline",
                      "value": "{0} to {1}".format(frm, payload["to_deadline"])})
        if payload.get("raw_text"):
            facts.append({"title": "From phrase",
                          "value": '"{0}"'.format(payload["raw_text"])})
    elif op == "reassign":
        facts.append({"title": "New owner", "value": payload["to_owner_ref"]})
    elif op == "add_blocker":
        facts.append({"title": "Blocked on", "value": payload["description"]})
    elif op in ("create_task", "add_subtask"):
        if payload.get("deadline"):
            facts.append({"title": "Deadline", "value": payload["deadline"]})
        if payload.get("match_note"):
            facts.append({"title": "Duplicate check",
                          "value": payload["match_note"]})
    elif op == "merge_tasks":
        facts.append({"title": "Why", "value": payload["judge_rationale"]})
    facts.append({"title": "Confidence",
                  "value": "{0:.0%}".format(prop.get("confidence", 0))})
    return facts


def _evidence_actions(prop):
    actions = []
    for i, ev in enumerate(prop.get("evidence", [])[:3]):
        url = ev.get("web_url")
        if not url:
            continue
        label = "View source" if i == 0 else "Source {0}".format(i + 1)
        actions.append({"type": "Action.OpenUrl", "title": label, "url": url})
    return actions


def _decide_action(packet, prop, title, decision):
    return {
        "type": "Action.Execute",
        "title": title,
        "verb": "decide",
        "data": {"packet_id": packet["packet_id"],
                 "proposal_id": prop["proposal_id"],
                 "decision": decision},
    }


def _reject_showcard(packet, prop):
    reason_id = "reject_reason_{0}".format(prop["proposal_id"])
    return {
        "type": "Action.ShowCard",
        "title": "Reject",
        "card": {
            "type": "AdaptiveCard",
            "body": [{
                "type": "Input.ChoiceSet", "id": reason_id,
                "style": "compact", "label": "Why? (tunes future proposals)",
                "value": "other", "choices": REJECT_REASON_CHOICES,
            }],
            "actions": [{
                "type": "Action.Execute", "title": "Confirm reject",
                "verb": "decide",
                "data": {"packet_id": packet["packet_id"],
                         "proposal_id": prop["proposal_id"],
                         "decision": "reject",
                         "reject_reason_input_id": reason_id},
            }],
        },
    }


def _edit_showcard(packet, prop):
    """Op-specific inline edit. Only ops with a meaningful single-field edit
    get one; everything else edits on the dashboard (deep link would go here)."""
    op = prop["op"]
    payload = prop["payload"]
    inputs = []
    if op == "update_deadline" or op in ("create_task", "add_subtask"):
        inputs.append({"type": "Input.Date", "id": "edit_deadline",
                       "label": "Deadline",
                       "value": payload.get("to_deadline")
                                or payload.get("deadline") or ""})
    if op in ("create_task", "add_subtask"):
        inputs.insert(0, {"type": "Input.Text", "id": "edit_title",
                          "label": "Title", "value": payload["title"]})
    if op == "update_status":
        inputs.append({
            "type": "Input.ChoiceSet", "id": "edit_status",
            "label": "Status", "style": "compact",
            "value": payload["to_status"],
            "choices": [{"title": s, "value": s} for s in
                        ("proposed", "in_progress", "blocked",
                         "completed", "cancelled")],
        })
    if not inputs:
        return None
    return {
        "type": "Action.ShowCard",
        "title": "Edit",
        "card": {
            "type": "AdaptiveCard",
            "body": inputs,
            "actions": [{
                "type": "Action.Execute", "title": "Accept with edits",
                "verb": "decide",
                "data": {"packet_id": packet["packet_id"],
                         "proposal_id": prop["proposal_id"],
                         "decision": "accept_edited"},
            }],
        },
    }


def _overflow_row(packet, overflow, page):
    return {
        "type": "ActionSet",
        "actions": [{
            "type": "Action.Execute",
            "title": "Show {0} more".format(overflow),
            "verb": "card/refresh",
            "data": {"packet_id": packet["packet_id"], "page": page + 1},
        }],
    }


def _op_color(op):
    if op in ("complete_task", "resolve_blocker"):
        return "Good"
    if op in ("cancel_task", "add_blocker", "merge_tasks"):
        return "Attention"
    return "Accent"


def _assert_size(card):
    size = len(json.dumps(card, separators=(",", ":")).encode("utf-8"))
    if size > MAX_CARD_BYTES:
        raise ValueError(
            "card is {0} bytes; exceeds Teams 28KB limit -- lower "
            "MAX_PROPOSALS_PER_CARD".format(size))
    return size
