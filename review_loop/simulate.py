"""End-to-end simulation of the review loop.

Exercises: tick with proposals, empty-tick silence, decision application,
accept_edited, defer signal-return, optimistic-concurrency invalidation,
supersede on next tick, expiry sweep, idempotent decisions, and card
rendering (delivered, paged, terminal) with size checks.

Run: python3 simulate.py
Python 3.8 compatible. Stdlib only.
"""

import datetime
import json

from orchestrator import Orchestrator, utcnow
import review_card


class FakeReconciler(object):
    def __init__(self):
        self.next_proposals = []

    def build_packet(self, user_ref, signals, board):
        return {"user_ref": user_ref, "cadence_run_id": "run-1",
                "proposals": list(self.next_proposals)}


class FakeBoardStore(object):
    def __init__(self):
        self.version = 41
        self.applied = []
        self.manually_closed = set()

    def get_board(self, user_ref):
        return {"version": self.version, "tasks": {}}

    def apply(self, user_ref, proposal, decision):
        self.applied.append((proposal["proposal_id"], decision["decision"]))
        self.version += 1
        return self.version

    def proposal_still_valid(self, proposal, board):
        if proposal.get("target_task_id") in self.manually_closed:
            return False, "task_already_closed_manually"
        return True, None


class FakeSignalStore(object):
    def __init__(self):
        self.pool = ["sig-1", "sig-2", "sig-3"]
        self.returned = []

    def take_unconsumed(self, user_ref):
        out, self.pool = self.pool, []
        return out

    def return_signals(self, user_ref, signal_ids):
        self.returned.extend(signal_ids)
        self.pool.extend(signal_ids)


class FakeTransport(object):
    def __init__(self):
        self.delivered = []
        self.refreshes = []

    def deliver_card(self, user_ref, packet):
        aid = "activity-{0}".format(len(self.delivered) + 1)
        self.delivered.append((aid, packet["packet_id"]))
        return aid

    def refresh_card(self, activity_id, record):
        self.refreshes.append((activity_id, record.state))


def make_proposals():
    return [
        {"proposal_id": "prop-1", "op": "update_status",
         "target_task_id": "task-88",
         "rationale": "In Tuesday standup you said the drift monitor deploy was underway.",
         "confidence": 0.85, "source_signals": ["sig-1"],
         "evidence": [{"source_type": "transcript_utterance",
                       "resource_id": "utt-442",
                       "web_url": "https://teams.microsoft.com/l/message/x/442"}],
         "conflict": None,
         "payload": {"kind": "update_status", "from_status": "proposed",
                     "to_status": "in_progress"}},
        {"proposal_id": "prop-2", "op": "update_deadline",
         "target_task_id": "task-88",
         "rationale": "You told Priya it would land by end of next sprint.",
         "confidence": 0.72, "source_signals": ["sig-2"],
         "evidence": [{"source_type": "teams_message", "resource_id": "msg-9",
                       "web_url": "https://teams.microsoft.com/l/message/x/9"}],
         "conflict": None,
         "payload": {"kind": "update_deadline", "from_deadline": None,
                     "to_deadline": "2026-07-24",
                     "raw_text": "end of next sprint"}},
        {"proposal_id": "prop-3", "op": "complete_task",
         "target_task_id": "task-70",
         "rationale": "Two independent mentions that the intake OCR fix shipped.",
         "confidence": 0.88, "source_signals": ["sig-3"],
         "evidence": [{"source_type": "email_message", "resource_id": "mail-4",
                       "web_url": "https://outlook.office.com/mail/4"}],
         "conflict": {"type": "contradicts_manual_edit",
                      "detail": "You reopened this task manually yesterday."},
         "payload": {"kind": "complete_task", "completion_note": None}},
    ]


def main():
    checks = 0

    def ok(cond, label):
        nonlocal checks
        assert cond, label
        checks += 1
        print("  pass: {0}".format(label))

    rec, board = FakeReconciler(), FakeBoardStore()
    sigs, transport = FakeSignalStore(), FakeTransport()
    log = []
    now = [utcnow()]
    orch = Orchestrator(rec, board, sigs, transport, log,
                        packet_ttl_hours=48, clock=lambda: now[0])

    print("tick 1: proposals present")
    rec.next_proposals = make_proposals()
    record = orch.run_cadence_tick("alex")
    ok(record is not None and record.state == "delivered", "packet delivered")
    ok(len(transport.delivered) == 1, "one card sent")
    packet = record.packet

    print("card render: delivered state")
    titles = {"task-88": "Ship drift monitoring", "task-70": "Fix intake OCR"}
    card = review_card.build_review_card(packet, task_titles=titles)
    ok(card["version"] == "1.5" and card["refresh"]["userIds"] == ["alex"],
       "AC 1.5 with refresh userIds")
    blob = json.dumps(card)
    ok(blob.index("prop-3") < blob.index("prop-1"),
       "conflicted proposal ordered first")
    ok("Confirm reject" in blob and "reject_reason_prop-1" in blob,
       "reject ShowCard with reason choices present")
    ok("Accept all unconflicted" in blob, "bulk action present")
    size = len(blob.encode("utf-8"))
    ok(size < review_card.MAX_CARD_BYTES,
       "card size {0}B under 28KB".format(size))

    print("decisions")
    r = orch.apply_decision({"proposal_id": "prop-1", "packet_id": packet["packet_id"],
                             "decision": "accept", "decided_by": "alex",
                             "decided_at": now[0].isoformat()})
    ok(r == "recorded" and board.applied[-1] == ("prop-1", "accept"),
       "accept applied to board")
    ok(record.state == "partially_decided", "state partially_decided")
    r = orch.apply_decision({"proposal_id": "prop-1", "packet_id": packet["packet_id"],
                             "decision": "accept", "decided_by": "alex",
                             "decided_at": now[0].isoformat()})
    ok(r == "duplicate_ignored" and len(board.applied) == 1,
       "duplicate decision idempotent")

    r = orch.apply_decision({"proposal_id": "prop-2", "packet_id": packet["packet_id"],
                             "decision": "accept_edited", "decided_by": "alex",
                             "edited_payload": {"kind": "update_deadline",
                                                "to_deadline": "2026-07-31"},
                             "decided_at": now[0].isoformat()})
    ok(r == "recorded" and board.applied[-1] == ("prop-2", "accept_edited"),
       "accept_edited applied")

    print("optimistic concurrency: board moved under prop-3")
    board.manually_closed.add("task-70")
    r = orch.apply_decision({"proposal_id": "prop-3", "packet_id": packet["packet_id"],
                             "decision": "accept", "decided_by": "alex",
                             "decided_at": now[0].isoformat()})
    ok(r.startswith("invalidated:task_already_closed"),
       "stale accept invalidated, not applied")
    ok(record.state == "closed", "packet closed when nothing undecided")
    ok(transport.refreshes[-1][1] == "closed", "card refreshed to closed")
    ok(len(log) == 3, "three decision records logged")

    print("tick 2: empty reconciliation is silent")
    rec.next_proposals = []
    ok(orch.run_cadence_tick("alex") is None and len(transport.delivered) == 1,
       "no card for empty packet")

    print("tick 3 + supersede")
    rec.next_proposals = make_proposals()[:2]
    sigs.pool = ["sig-10", "sig-11"]
    rec3 = orch.run_cadence_tick("alex")
    ok(rec3.state == "delivered", "third packet delivered")
    rec.next_proposals = [make_proposals()[0]]
    sigs.pool = ["sig-20"]
    rec4 = orch.run_cadence_tick("alex")
    ok(any(s == "superseded" for _, s in transport.refreshes),
       "open packet superseded on new tick")
    ok("sig-1" in sigs.returned or "sig-2" in sigs.returned,
       "undecided signals returned to pool")

    print("expiry sweep")
    now[0] = now[0] + datetime.timedelta(hours=49)
    expired = orch.expire_sweep()
    ok(len(expired) == 1 and expired[0].state == "expired",
       "overdue packet expired")
    ok("sig-1" in sigs.pool, "expired packet signals back in pool")

    print("terminal card render")
    tcard = review_card.build_terminal_card(packet, "closed",
                                            "2 accepted, 1 invalidated")
    ok(tcard["body"][1]["text"].startswith("2 accepted"), "terminal card renders")

    print("pagination: 20 proposals")
    big = {"packet_id": "pkt-big", "user_ref": "alex", "board_version": 1,
           "proposals": []}
    for i in range(20):
        p = json.loads(json.dumps(make_proposals()[0]))
        p["proposal_id"] = "prop-{0}".format(100 + i)
        big["proposals"].append(p)
    page0 = review_card.build_review_card(big, page=0, task_titles=titles)
    ok("Show 12 more" in json.dumps(page0), "overflow row pages correctly")

    print("\nall {0} checks passed".format(checks))


if __name__ == "__main__":
    main()
