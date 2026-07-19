"""Tests for graph_ingestion.py and bot_host.py.

The bot tests run against the REAL Orchestrator and review_card from
review_loop/ -- only Teams and Graph are faked. Run: python3 test_integration.py
"""

import datetime
import json
import sys

sys.path.insert(0, "../ingestion")
sys.path.insert(0, "../bot")
sys.path.insert(0, "../../review_loop")

import graph_ingestion as gi
import bot_host
from orchestrator import Orchestrator, utcnow
from simulate import (FakeReconciler, FakeBoardStore, FakeSignalStore,
                      make_proposals)

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


# ------------------------------------------------------------------ ingestion

class FakeGraph(object):
    def __init__(self):
        self.posts, self.patches, self.n = [], [], 0

    def post(self, path, body):
        self.posts.append((path, body))
        self.n += 1
        return {"id": "sub-{0}".format(self.n),
                "expirationDateTime": body["expirationDateTime"]}

    def patch(self, path, body):
        self.patches.append((path, body))
        return {"expirationDateTime": body["expirationDateTime"]}


class FakeQueue(object):
    def __init__(self):
        self.jobs = []

    def enqueue(self, job):
        self.jobs.append(job)


def test_ingestion():
    print("ingestion: subscriptions")
    now = [utcnow()]
    graph, store = FakeGraph(), {}
    mgr = gi.SubscriptionManager(graph, store,
                                 "https://ourapp/api/notifications",
                                 clock=lambda: now[0])
    created = mgr.ensure_tenant_subscriptions()
    ok(len(created) == 2 and len(mgr.ensure_tenant_subscriptions()) == 0,
       "tenant subs created once, idempotent")
    mail = mgr.ensure_mail_subscription("alex")
    ok(mail["resource"] == "/users/alex/messages", "per-user mail sub")
    ok(all(len(r["client_state"]) > 20 for r in store.values()),
       "random clientState per subscription")

    now[0] = now[0] + datetime.timedelta(days=2)
    renewed = mgr.renew_due()
    ok(len(renewed) == 2 and len(mgr.renew_due()) == 0,
       "half-life renewal: 2 teams subs due at 2 days, mail not, idempotent")

    print("ingestion: webhook handler")
    queue = FakeQueue()
    batcher = gi.WindowBatcher(queue, quiet_seconds=180,
                               max_window_minutes=30, clock=lambda: now[0])
    handler = gi.WebhookHandler(mgr, batcher)

    status, body = handler.handle({"validationToken": "tok-123"}, "")
    ok(status == 200 and body == "tok-123", "validation handshake echoes token")

    teams_sub = [r for r in store.values()
                 if r["source_type"] == "teams_channel"][0]

    def note(msg_id, client_state, lifecycle=None):
        n = {"subscriptionId": teams_sub["subscription_id"],
             "clientState": client_state,
             "resource": "teams('t1')/channels('c1')/messages('%s')" % msg_id,
             "resourceData": {"id": msg_id}}
        if lifecycle:
            n["lifecycleEvent"] = lifecycle
        return json.dumps({"value": [n]})

    status, _ = handler.handle({}, note("m1", "WRONG"))
    ok(status == 202 and handler.dropped_bad_state == 1
       and not batcher.open_windows, "bad clientState dropped, still 202")

    handler.handle({}, note("m1", teams_sub["client_state"]))
    handler.handle({}, note("m2", teams_sub["client_state"]))
    handler.handle({}, note("m2", teams_sub["client_state"]))
    key = ("teams_channel", "teams('t1')/channels('c1')")
    ok(key in batcher.open_windows
       and batcher.open_windows[key]["resource_ids"] == ["m1", "m2"],
       "notifications windowed by conversation, deduped")

    ok(batcher.flush_due() == [], "window stays open during burst")
    now[0] = now[0] + datetime.timedelta(seconds=200)
    flushed = batcher.flush_due()
    ok(len(flushed) == 1 and flushed[0]["resource_ids"] == ["m1", "m2"]
       and queue.jobs[0]["source_type"] == "teams_channel",
       "quiet-period flush enqueues one extraction job")

    before = len(graph.patches)
    handler.handle({}, note("x", teams_sub["client_state"],
                            lifecycle="reauthorizationRequired"))
    ok(len(graph.patches) == before + 1, "reauthorizationRequired renews")
    handler.handle({}, note("x", teams_sub["client_state"],
                            lifecycle="subscriptionRemoved"))
    ok(teams_sub["subscription_id"] not in store,
       "subscriptionRemoved drops the record for recreation")

    plan = mgr.transcript_poll_plan([
        {"online_meeting_id": "mtg-1", "start": "2026-07-16T17:00:00Z",
         "end": "2026-07-16T17:30:00Z"}])
    ok(plan[0]["source_type"] == "meeting_transcript",
       "transcript poll plan emitted for ended meetings")


# ------------------------------------------------------------------ bot host

class FakeConnector(object):
    def __init__(self):
        self.sent, self.updated, self.n = [], [], 0

    def send(self, conversation_id, service_url, activity):
        self.n += 1
        aid = "act-{0}".format(self.n)
        self.sent.append((conversation_id, aid, activity))
        return aid

    def update(self, conversation_id, service_url, activity_id, activity):
        self.updated.append((activity_id, activity))


def install_activity(aad, conv="conv-1"):
    return {"type": "conversationUpdate",
            "membersAdded": [{"id": "29:userid", "aadObjectId": aad},
                             {"id": "28:botid"}],
            "recipient": {"id": "28:botid"},
            "conversation": {"id": conv, "tenantId": "tenant-1"},
            "serviceUrl": "https://smba.trafficmanager.net/amer/"}


def invoke_activity(aad, verb, data, inputs=None):
    return {"type": "invoke", "name": "adaptiveCard/action",
            "from": {"aadObjectId": aad},
            "conversation": {"id": "conv-1"},
            "serviceUrl": "https://smba.trafficmanager.net/amer/",
            "value": {"action": {"verb": verb, "data": data},
                      "inputs": inputs or {}}}


def test_bot():
    print("bot: conversation reference capture")
    refs = bot_host.ConversationReferenceStore()
    captured = refs.capture(install_activity("alex"))
    ok(captured == ["alex"] and "alex" in refs.refs
       and refs.refs["alex"]["conversation_id"] == "conv-1",
       "install captures user ref, skips bot")

    print("bot: full loop with real orchestrator")
    now = [utcnow()]
    connector = FakeConnector()
    titles = {"task-88": "Ship drift monitoring", "task-70": "Fix intake OCR"}
    transport = bot_host.TeamsTransport(connector, refs,
                                        task_titles_lookup=lambda p: titles)
    rec, board, sigs = FakeReconciler(), FakeBoardStore(), FakeSignalStore()
    orch = Orchestrator(rec, board, sigs, transport, [],
                        packet_ttl_hours=48, clock=lambda: now[0])
    router = bot_host.InvokeRouter(orch, transport, clock=lambda: now[0])

    rec.next_proposals = make_proposals()
    record = orch.run_cadence_tick("alex")
    ok(len(connector.sent) == 1 and connector.sent[0][2]["attachments"][0]
       ["contentType"] == bot_host.CARD_CONTENT_TYPE,
       "proactive delivery sends an adaptive card activity")

    pkt = record.packet["packet_id"]

    print("bot: identity comes from the activity, never card data")
    resp = router.handle(invoke_activity(
        "mallory", "decide",
        {"packet_id": pkt, "proposal_id": "prop-1", "decision": "accept"}))
    ok("someone else" in resp["value"] and len(board.applied) == 0,
       "non-owner actor refused; nothing applied")

    spoofed = invoke_activity("alex", "decide",
                              {"packet_id": pkt, "proposal_id": "prop-1",
                               "decision": "accept",
                               "decided_by": "mallory"})
    router.handle(spoofed)
    ok(record.decisions["prop-1"]["decided_by"] == "alex",
       "decided_by stamped from authenticated identity, spoof field ignored")
    ok(len(connector.updated) >= 1, "card refreshed after decision")

    print("bot: reject reason read from ShowCard inputs")
    router.handle(invoke_activity(
        "alex", "decide",
        {"packet_id": pkt, "proposal_id": "prop-2", "decision": "reject",
         "reject_reason_input_id": "reject_reason_prop-2"},
        inputs={"reject_reason_prop-2": "bad_date_resolution"}))
    ok(record.decisions["prop-2"]["reject_reason"] == "bad_date_resolution",
       "enumerated reject reason captured")

    print("bot: bulk excludes conflicted, expands to individual decisions")
    resp = router.handle(invoke_activity(
        "alex", "bulk", {"packet_id": pkt, "action": "accept_unconflicted"}))
    ok("prop-3" not in [d["proposal_id"] for d in record.decisions.values()
                        if d["decision"].startswith("accept")]
       or record.decisions.get("prop-3", {}).get("decision") != "accept",
       "conflicted prop-3 untouched by bulk accept")
    router.handle(invoke_activity(
        "alex", "bulk", {"packet_id": pkt, "action": "defer_all"}))
    ok(record.decisions["prop-3"]["decision"] == "defer",
       "defer_all sweeps the conflicted leftover")
    ok(record.state == "closed", "packet closed after all decided")
    ok(resp["type"] == "application/vnd.microsoft.card.adaptive",
       "invoke response carries re-rendered card")

    print("bot: terminal card render for closed packet")
    final = router.handle(invoke_activity("alex", "card/refresh",
                                          {"packet_id": pkt}))
    body_text = json.dumps(final["value"])
    ok("accepted" in body_text and "deferred" in body_text,
       "closed packet renders receipt summary")


if __name__ == "__main__":
    test_ingestion()
    test_bot()
    print("\nall {0} integration checks passed".format(CHECKS[0]))
