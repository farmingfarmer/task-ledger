"""Bot host: wires orchestrator.py to real Teams conversations.

Three pieces:

  ConversationReferenceStore   captures conversationReference on install
                               (conversationUpdate/membersAdded) so proactive
                               delivery works after restarts.
  TeamsTransport               implements the orchestrator's transport duck
                               type (deliver_card / refresh_card) over the Bot
                               Connector REST API via an injected connector.
  InvokeRouter                 handles adaptiveCard/action invoke activities:
                               maps card verbs to DecisionRecords, stamps
                               decided_by from the AUTHENTICATED activity
                               identity (never card data), expands bulk verbs
                               server-side, and returns the re-rendered card.

The connector is injected as three callables so tests run without Teams and
production can use either raw Connector REST or the botbuilder SDK:

  connector.send(conversation_id, service_url, activity)   -> activity_id
  connector.update(conversation_id, service_url, activity_id, activity) -> None

Wire-up in an aiohttp/Functions endpoint is ~20 lines: authenticate the
inbound request (Bot Framework JWT), parse the Activity, and route:
  activity.type == 'conversationUpdate' -> ref_store.capture(activity)
  activity.type == 'invoke' and activity.name == 'adaptiveCard/action'
      -> InvokeRouter.handle(activity)  (return its dict as the invoke response)

Python 3.8 compatible. Stdlib only. Imports review_card for rendering.
"""

import copy
import json

import review_card


CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


class ConversationReferenceStore(object):
    """user_ref (AAD object id) -> conversation reference.

    Backed by a dict here; persist to a table in production (losing these
    means you cannot message users until they talk to the bot again)."""

    def __init__(self, backing=None):
        self.refs = backing if backing is not None else {}

    def capture(self, activity):
        """Call for every inbound activity; personal-scope installs arrive as
        conversationUpdate with the user in membersAdded."""
        captured = []
        members = activity.get("membersAdded") or []
        bot_id = (activity.get("recipient") or {}).get("id")
        for m in members:
            if m.get("id") == bot_id:
                continue
            aad = m.get("aadObjectId")
            if not aad:
                continue
            self.refs[aad] = {
                "conversation_id": activity["conversation"]["id"],
                "service_url": activity["serviceUrl"],
                "tenant_id": (activity.get("conversation") or {})
                             .get("tenantId"),
            }
            captured.append(aad)
        # Any 1:1 message also refreshes the reference.
        frm = activity.get("from") or {}
        if activity.get("type") == "message" and frm.get("aadObjectId"):
            self.refs[frm["aadObjectId"]] = {
                "conversation_id": activity["conversation"]["id"],
                "service_url": activity["serviceUrl"],
                "tenant_id": (activity.get("conversation") or {})
                             .get("tenantId"),
            }
            captured.append(frm["aadObjectId"])
        return captured


class TeamsTransport(object):
    """Orchestrator transport over Bot Connector proactive messaging."""

    def __init__(self, connector, ref_store, task_titles_lookup=None):
        self.connector = connector
        self.refs = ref_store
        self.task_titles_lookup = task_titles_lookup or (lambda packet: {})
        self.sent = {}   # activity_id -> (user_ref, packet_id) for refresh

    def deliver_card(self, user_ref, packet):
        ref = self.refs.refs.get(user_ref)
        if ref is None:
            raise LookupError(
                "no conversation reference for {0}; user has not installed "
                "the bot".format(user_ref))
        card = review_card.build_review_card(
            packet, task_titles=self.task_titles_lookup(packet))
        activity = _card_activity(card)
        activity_id = self.connector.send(
            ref["conversation_id"], ref["service_url"], activity)
        self.sent[activity_id] = (user_ref, packet["packet_id"])
        return activity_id

    def refresh_card(self, activity_id, packet_record):
        info = self.sent.get(activity_id)
        if info is None:
            return
        user_ref, _ = info
        ref = self.refs.refs.get(user_ref)
        if ref is None:
            return
        card = _render_for_state(packet_record, self.task_titles_lookup)
        self.connector.update(
            ref["conversation_id"], ref["service_url"], activity_id,
            _card_activity(card))


def _card_activity(card):
    return {
        "type": "message",
        "attachments": [{"contentType": CARD_CONTENT_TYPE, "content": card}],
    }


def _render_for_state(record, titles_lookup):
    state = record.state
    packet = record.packet
    if state in ("delivered", "partially_decided"):
        remaining = {
            "packet_id": packet["packet_id"],
            "user_ref": packet["user_ref"],
            "board_version": packet["board_version"],
            "proposals": [p for p in packet["proposals"]
                          if p["proposal_id"] in
                          set(record.undecided_proposal_ids())],
        }
        if remaining["proposals"]:
            return review_card.build_review_card(
                remaining, task_titles=titles_lookup(packet))
        state = "closed"
    counts = _decision_counts(record)
    if state == "closed":
        line = ("{0} accepted, {1} rejected, {2} deferred"
                .format(counts["accepted"], counts["rejected"],
                        counts["deferred"]))
        if record.invalidated:
            line += ", {0} no longer applicable".format(len(record.invalidated))
    elif state == "superseded":
        line = "Superseded by a newer review. Undecided items were re-checked."
    else:
        line = "This review expired."
    return review_card.build_terminal_card(packet, state, line)


def _decision_counts(record):
    out = {"accepted": 0, "rejected": 0, "deferred": 0}
    for d in record.decisions.values():
        if d["decision"] in ("accept", "accept_edited"):
            out["accepted"] += 1
        elif d["decision"] == "reject":
            out["rejected"] += 1
        elif d["decision"] == "defer":
            out["deferred"] += 1
    return out


class InvokeRouter(object):
    """Handles adaptiveCard/action invoke activities.

    Security rules enforced here:
      - decided_by comes from activity.from.aadObjectId (authenticated by the
        Bot Framework JWT on the endpoint), NEVER from card data.
      - The actor must be the packet owner; anyone else in the thread gets a
        polite refusal even though personal-scope makes this unlikely.
      - Bulk verbs expand into individual DecisionRecords server-side so the
        audit log never contains compound entries.
    """

    def __init__(self, orchestrator, transport, clock):
        self.orchestrator = orchestrator
        self.transport = transport
        self.clock = clock

    def handle(self, activity):
        actor = (activity.get("from") or {}).get("aadObjectId")
        value = activity.get("value") or {}
        action = value.get("action") or {}
        verb = action.get("verb")
        data = action.get("data") or {}
        inputs = value.get("inputs") or action.get("inputs") or {}

        if actor is None:
            return _invoke_message("Could not verify your identity.")

        record = self._record_for(data.get("packet_id"))
        if record is None:
            return _invoke_message("This review is no longer active.")
        if record.packet["user_ref"] != actor:
            return _invoke_message("This review belongs to someone else.")

        if verb == "card/refresh":
            return self._card_response(record)
        if verb == "decide":
            self._apply_one(record, actor, data, inputs)
            return self._card_response(record)
        if verb == "bulk":
            self._apply_bulk(record, actor, data)
            return self._card_response(record)
        return _invoke_message("Unrecognized action.")

    # -------------------------------------------------------------- internal

    def _record_for(self, packet_id):
        for record in self.orchestrator.open_packets.values():
            if record.packet["packet_id"] == packet_id:
                return record
        for record in self.orchestrator.history:
            if record.packet["packet_id"] == packet_id:
                return record
        return None

    def _apply_one(self, record, actor, data, inputs):
        decision = {
            "proposal_id": data["proposal_id"],
            "packet_id": data["packet_id"],
            "decision": data["decision"],
            "decided_by": actor,                       # authenticated identity
            "decided_at": self.clock().isoformat(),
        }
        if data["decision"] == "reject":
            reason_key = data.get("reject_reason_input_id", "")
            decision["reject_reason"] = inputs.get(reason_key, "other")
        if data["decision"] == "accept_edited":
            decision["edited_payload"] = self._edited_payload(record, data,
                                                              inputs)
        if record.state in ("delivered", "partially_decided"):
            self.orchestrator.apply_decision(decision)

    def _edited_payload(self, record, data, inputs):
        proposal = None
        for p in record.packet["proposals"]:
            if p["proposal_id"] == data["proposal_id"]:
                proposal = p
                break
        payload = copy.deepcopy(proposal["payload"]) if proposal else {}
        if inputs.get("edit_title"):
            payload["title"] = inputs["edit_title"]
        if inputs.get("edit_deadline"):
            if "to_deadline" in payload:
                payload["to_deadline"] = inputs["edit_deadline"]
            else:
                payload["deadline"] = inputs["edit_deadline"]
        if inputs.get("edit_status"):
            payload["to_status"] = inputs["edit_status"]
        return payload

    def _apply_bulk(self, record, actor, data):
        which = data.get("action")
        for p in list(record.packet["proposals"]):
            pid = p["proposal_id"]
            if pid in record.decisions or pid in record.invalidated:
                continue
            if which == "accept_unconflicted":
                if p.get("conflict"):
                    continue
                decision_kind = "accept"
            elif which == "defer_all":
                decision_kind = "defer"
            else:
                return
            if record.state not in ("delivered", "partially_decided"):
                break
            self.orchestrator.apply_decision({
                "proposal_id": pid,
                "packet_id": data["packet_id"],
                "decision": decision_kind,
                "decided_by": actor,
                "decided_at": self.clock().isoformat(),
            })

    def _card_response(self, record):
        card = _render_for_state(record,
                                 self.transport.task_titles_lookup)
        return {
            "statusCode": 200,
            "type": "application/vnd.microsoft.card.adaptive",
            "value": card,
        }


def _invoke_message(text):
    return {"statusCode": 200,
            "type": "application/vnd.microsoft.activity.message",
            "value": text}
