"""Cadence orchestrator for the review loop.

Owns the packet lifecycle:

    draft -> delivered -> partially_decided -> closed
                 |                |
                 +--> expired     +--> expired
                 +--> superseded  +--> superseded

Responsibilities:
  - Run a cadence tick per user: collect unconsumed signals, invoke the
    reconciler, build a ReviewPacket, deliver it (or skip silently if empty).
  - Apply DecisionRecords with optimistic concurrency against board_version;
    rebase or invalidate proposals whose board context changed underneath them.
  - Supersede still-open packets when a new cadence run fires: undecided
    proposals are NOT copied forward -- their source signals are returned to
    the unconsumed pool and re-reconciled against the current board, which is
    strictly more correct than replaying a stale diff.
  - Expiry sweep for packets past expires_at (same signal-return semantics).

Transport, reconciliation, and storage are injected. In production:
  scheduler  -> Azure Functions timer / Durable Functions per-user orchestration
  transport  -> Bot Framework proactive message with stored conversationReference
  repo       -> the append-only tables from CONTRACTS.md
Here they are in-memory protocols so the state machine is testable standalone.

Python 3.8 compatible. Stdlib only. No emojis.
"""

import datetime
import uuid


PACKET_STATES = (
    "draft",
    "delivered",
    "partially_decided",
    "closed",
    "expired",
    "superseded",
)

TERMINAL_STATES = ("closed", "expired", "superseded")

_VALID_TRANSITIONS = {
    "draft": {"delivered", "expired"},
    "delivered": {"partially_decided", "closed", "expired", "superseded"},
    "partially_decided": {"closed", "expired", "superseded"},
}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class PacketRecord(object):
    """Mutable orchestrator-side wrapper around a ReviewPacket."""

    def __init__(self, packet):
        self.packet = packet
        self.state = "draft"
        self.decisions = {}          # proposal_id -> DecisionRecord
        self.invalidated = {}        # proposal_id -> reason
        self.delivered_at = None
        self.card_activity_id = None # transport handle for card refresh

    def undecided_proposal_ids(self):
        out = []
        for p in self.packet["proposals"]:
            pid = p["proposal_id"]
            if pid not in self.decisions and pid not in self.invalidated:
                out.append(pid)
        return out

    def transition(self, new_state):
        allowed = _VALID_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                "illegal packet transition {0} -> {1}".format(self.state, new_state)
            )
        self.state = new_state


class Orchestrator(object):
    """Coordinates cadence ticks, delivery, decisions, rebase, and expiry.

    Injected collaborators (duck-typed):
      reconciler.build_packet(user_ref, signals, board) -> ReviewPacket dict
                 (returns packet with proposals possibly empty)
      board_store.get_board(user_ref) -> {"version": int, "tasks": {...}}
      board_store.apply(user_ref, proposal, decision) -> new_version (int)
      board_store.proposal_still_valid(proposal, board) -> (bool, reason_or_None)
      signal_store.take_unconsumed(user_ref) -> list of signals
      signal_store.return_signals(user_ref, signal_ids) -> None
      transport.deliver_card(user_ref, packet) -> activity_id
      transport.refresh_card(activity_id, packet_record) -> None
      decision_log.append(decision) -> None
      clock() -> aware datetime (injectable for tests)
    """

    def __init__(self, reconciler, board_store, signal_store, transport,
                 decision_log, packet_ttl_hours=48, clock=utcnow):
        self.reconciler = reconciler
        self.board_store = board_store
        self.signal_store = signal_store
        self.transport = transport
        self.decision_log = decision_log
        self.packet_ttl = datetime.timedelta(hours=packet_ttl_hours)
        self.clock = clock
        self.open_packets = {}   # user_ref -> PacketRecord
        self.history = []        # terminal PacketRecords, for audit

    # ------------------------------------------------------------------ tick

    def run_cadence_tick(self, user_ref):
        """One cadence firing for one user. Returns the new PacketRecord or
        None when there was nothing to review (silent skip: no empty cards)."""
        self._supersede_open_packet(user_ref)

        signals = self.signal_store.take_unconsumed(user_ref)
        board = self.board_store.get_board(user_ref)
        packet = self.reconciler.build_packet(user_ref, signals, board)

        if not packet["proposals"]:
            # Consumed signals that produced no proposals stay consumed --
            # they matched already-confirmed state. Silence is the output.
            return None

        packet["packet_id"] = packet.get("packet_id") or str(uuid.uuid4())
        packet["board_version"] = board["version"]
        packet["generated_at"] = self.clock().isoformat()
        packet["expires_at"] = (self.clock() + self.packet_ttl).isoformat()

        record = PacketRecord(packet)
        record.card_activity_id = self.transport.deliver_card(user_ref, packet)
        record.delivered_at = self.clock()
        record.transition("delivered")
        self.open_packets[user_ref] = record
        return record

    def _supersede_open_packet(self, user_ref):
        record = self.open_packets.get(user_ref)
        if record is None or record.state in TERMINAL_STATES:
            return
        undecided = record.undecided_proposal_ids()
        signal_ids = _collect_source_signals(record.packet, undecided)
        if signal_ids:
            self.signal_store.return_signals(user_ref, signal_ids)
        record.transition("superseded")
        self.transport.refresh_card(record.card_activity_id, record)
        self._retire(user_ref, record)

    # ------------------------------------------------------------- decisions

    def apply_decision(self, decision):
        """Apply one DecisionRecord. Idempotent on (packet_id, proposal_id).

        Optimistic concurrency: before applying an accept, the proposal is
        re-validated against the CURRENT board. If the board moved and the
        proposal no longer makes sense (task closed manually, status already
        changed), it is invalidated rather than blind-applied, and the card
        is refreshed to show that."""
        user_ref = decision["decided_by"]
        record = self.open_packets.get(user_ref)
        if record is None or record.packet["packet_id"] != decision["packet_id"]:
            raise LookupError("no open packet {0} for user {1}".format(
                decision["packet_id"], user_ref))
        if record.state in TERMINAL_STATES:
            raise ValueError("packet {0} is {1}; decisions closed".format(
                decision["packet_id"], record.state))

        pid = decision["proposal_id"]
        if pid in record.decisions:
            return "duplicate_ignored"
        proposal = _find_proposal(record.packet, pid)
        if proposal is None:
            raise LookupError("proposal {0} not in packet".format(pid))
        if pid in record.invalidated:
            return "invalidated:{0}".format(record.invalidated[pid])

        outcome = "recorded"
        if decision["decision"] in ("accept", "accept_edited"):
            board = self.board_store.get_board(user_ref)
            still_valid, reason = self.board_store.proposal_still_valid(
                proposal, board)
            if not still_valid:
                record.invalidated[pid] = reason or "board_moved"
                outcome = "invalidated:{0}".format(record.invalidated[pid])
            else:
                self.board_store.apply(user_ref, proposal, decision)
        elif decision["decision"] == "defer":
            # Deferred proposals go back to the signal pool so the next
            # cadence re-reconciles them against the then-current board.
            self.signal_store.return_signals(
                user_ref, list(proposal.get("source_signals", [])))
            outcome = "deferred"

        record.decisions[pid] = decision
        self.decision_log.append(decision)

        if not record.undecided_proposal_ids():
            record.transition("closed")
            self.transport.refresh_card(record.card_activity_id, record)
            self._retire(user_ref, record)
        else:
            if record.state == "delivered":
                record.transition("partially_decided")
            self.transport.refresh_card(record.card_activity_id, record)
        return outcome

    # ---------------------------------------------------------------- expiry

    def expire_sweep(self):
        """Run periodically (independent of cadence). Expires overdue packets
        and returns their undecided signals to the pool."""
        expired = []
        for user_ref, record in list(self.open_packets.items()):
            if record.state in TERMINAL_STATES:
                continue
            expires_at = datetime.datetime.fromisoformat(
                record.packet["expires_at"])
            if self.clock() >= expires_at:
                undecided = record.undecided_proposal_ids()
                signal_ids = _collect_source_signals(record.packet, undecided)
                if signal_ids:
                    self.signal_store.return_signals(user_ref, signal_ids)
                record.transition("expired")
                self.transport.refresh_card(record.card_activity_id, record)
                self._retire(user_ref, record)
                expired.append(record)
        return expired

    def _retire(self, user_ref, record):
        self.history.append(record)
        if self.open_packets.get(user_ref) is record:
            del self.open_packets[user_ref]


def _find_proposal(packet, proposal_id):
    for p in packet["proposals"]:
        if p["proposal_id"] == proposal_id:
            return p
    return None


def _collect_source_signals(packet, proposal_ids):
    wanted = set(proposal_ids)
    out = []
    for p in packet["proposals"]:
        if p["proposal_id"] in wanted:
            for sid in p.get("source_signals", []):
                if sid not in out:
                    out.append(sid)
    return out
