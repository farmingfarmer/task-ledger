"""Microsoft Graph ingestion: change-notification subscriptions feeding the
extraction queue.

Three pieces:

  SubscriptionManager   creates/renews Graph change-notification subscriptions
                        for the communication surfaces that support them, and
                        tracks a polling plan for the ones that don't.
  WebhookHandler        the HTTP-facing logic: validation handshake, clientState
                        verification, lifecycle notifications, and fan-out of
                        resource notifications into the window batcher.
  WindowBatcher         groups notification bursts into extraction windows
                        (the ExtractionBatch unit from CONTRACTS.md) using a
                        quiet-period debounce, and enqueues extraction jobs.

Surfaces and how each is ingested:

  Teams channel messages   subscription: /teams/getAllMessages   (tenant-level)
  Teams chat messages      subscription: /chats/getAllMessages   (tenant-level)
  User mail                subscription: /users/{id}/messages
  Meeting transcripts      NO change notifications -- polled via the Meeting
                           Insights / onlineMeetings surface after calendar
                           events end (poll plan emitted by the manager; the
                           scheduler runs it alongside the cadence ticks).

Security posture:
  - Every subscription carries a per-subscription random clientState; every
    incoming notification must echo it or it is dropped and counted. This is
    the only authentication Graph gives plain notifications.
  - Notifications are treated as id-only triggers. Message CONTENT is fetched
    by the extraction worker under application permissions at processing time
    (pointer-not-copy discipline starts here: the queue never carries text).
  - Handler responds fast (Graph requires < 3s / 202) -- all real work is a
    queue append.

Transport (HTTP server, Graph client, queue) is injected; this module is the
logic and is tested standalone. Python 3.8 compatible. Stdlib only.
"""

import datetime
import json
import secrets
import uuid


GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph maximum subscription lifetimes differ per resource (chatMessage
# resources max out near 3 days; mail near 7). Renew at half-life.
MAX_LIFETIME_MINUTES = {
    "teams_channel": 4230,   # chatMessage limit (~2.9 days)
    "teams_chat": 4230,
    "email": 10070,          # ~7 days
}

SOURCE_RESOURCES = {
    "teams_channel": "/teams/getAllMessages",
    "teams_chat": "/chats/getAllMessages",
    # email is per-user: /users/{id}/messages, expanded at create time
}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class SubscriptionManager(object):
    """Creates and renews Graph subscriptions; owns clientState secrets.

    graph.post(path, body) / graph.patch(path, body) are injected and return
    parsed JSON. store persists subscription rows across restarts."""

    def __init__(self, graph, store, notification_url, clock=utcnow):
        self.graph = graph
        self.store = store            # dict-like: sub_id -> record
        self.notification_url = notification_url
        self.clock = clock

    def ensure_tenant_subscriptions(self):
        """Idempotently create the tenant-level Teams subscriptions."""
        created = []
        for source_type, resource in SOURCE_RESOURCES.items():
            if any(r["source_type"] == source_type
                   for r in self.store.values()):
                continue
            created.append(self._create(source_type, resource))
        return created

    def ensure_mail_subscription(self, user_ref):
        for r in self.store.values():
            if r["source_type"] == "email" and r["user_ref"] == user_ref:
                return r
        return self._create("email",
                            "/users/{0}/messages".format(user_ref),
                            user_ref=user_ref)

    def _create(self, source_type, resource, user_ref=None):
        client_state = secrets.token_urlsafe(32)
        minutes = MAX_LIFETIME_MINUTES[source_type]
        expiry = self.clock() + datetime.timedelta(minutes=minutes)
        body = {
            "changeType": "created",
            "notificationUrl": self.notification_url,
            "lifecycleNotificationUrl": self.notification_url,
            "resource": resource,
            "expirationDateTime": expiry.isoformat(),
            "clientState": client_state,
        }
        resp = self.graph.post("/subscriptions", body)
        record = {
            "subscription_id": resp["id"],
            "source_type": source_type,
            "resource": resource,
            "user_ref": user_ref,
            "client_state": client_state,
            "expires_at": resp["expirationDateTime"],
        }
        self.store[resp["id"]] = record
        return record

    def renew_due(self):
        """Renew every subscription past its half-life. Run on a timer."""
        renewed = []
        for sub_id, rec in list(self.store.items()):
            expires = datetime.datetime.fromisoformat(rec["expires_at"])
            minutes = MAX_LIFETIME_MINUTES[rec["source_type"]]
            half_life = expires - datetime.timedelta(minutes=minutes // 2)
            if self.clock() >= half_life:
                new_expiry = self.clock() + datetime.timedelta(minutes=minutes)
                resp = self.graph.patch(
                    "/subscriptions/{0}".format(sub_id),
                    {"expirationDateTime": new_expiry.isoformat()})
                rec["expires_at"] = resp["expirationDateTime"]
                renewed.append(sub_id)
        return renewed

    def reauthorize(self, sub_id):
        """Handle a reauthorizationRequired lifecycle event."""
        rec = self.store.get(sub_id)
        if rec is None:
            return None
        return self.renew_one(sub_id)

    def renew_one(self, sub_id):
        rec = self.store[sub_id]
        minutes = MAX_LIFETIME_MINUTES[rec["source_type"]]
        new_expiry = self.clock() + datetime.timedelta(minutes=minutes)
        resp = self.graph.patch(
            "/subscriptions/{0}".format(sub_id),
            {"expirationDateTime": new_expiry.isoformat()})
        rec["expires_at"] = resp["expirationDateTime"]
        return rec

    def transcript_poll_plan(self, ended_meetings):
        """Meeting transcripts have no change notifications. Given calendar
        events that ended since the last poll, emit fetch jobs for the
        extraction worker (which calls the Meeting Insights API)."""
        return [{
            "job_id": str(uuid.uuid4()),
            "source_type": "meeting_transcript",
            "source_ref": m["online_meeting_id"],
            "window_start": m["start"],
            "window_end": m["end"],
        } for m in ended_meetings]


class WindowBatcher(object):
    """Groups notification bursts into extraction windows.

    A Teams thread generates one notification per message; extracting per
    message wastes LLM calls and loses conversational context. The batcher
    holds an open window per (source_type, source_ref) and flushes it to the
    extraction queue when either:
      - quiet_seconds pass with no new notification (the burst ended), or
      - the window reaches max_window_minutes (long-running threads still
        flow through at bounded latency).
    """

    def __init__(self, queue, quiet_seconds=180, max_window_minutes=30,
                 clock=utcnow):
        self.queue = queue            # .enqueue(job_dict)
        self.quiet = datetime.timedelta(seconds=quiet_seconds)
        self.max_window = datetime.timedelta(minutes=max_window_minutes)
        self.clock = clock
        self.open_windows = {}        # (source_type, source_ref) -> window

    def add(self, source_type, source_ref, resource_id):
        key = (source_type, source_ref)
        now = self.clock()
        w = self.open_windows.get(key)
        if w is None:
            w = {"opened_at": now, "last_at": now, "resource_ids": []}
            self.open_windows[key] = w
        w["last_at"] = now
        if resource_id not in w["resource_ids"]:
            w["resource_ids"].append(resource_id)

    def flush_due(self):
        """Run on a short timer. Flushes quiet or aged windows."""
        flushed = []
        now = self.clock()
        for key, w in list(self.open_windows.items()):
            quiet_done = now - w["last_at"] >= self.quiet
            aged_out = now - w["opened_at"] >= self.max_window
            if quiet_done or aged_out:
                source_type, source_ref = key
                job = {
                    "job_id": str(uuid.uuid4()),
                    "source_type": source_type,
                    "source_ref": source_ref,
                    "window_start": w["opened_at"].isoformat(),
                    "window_end": w["last_at"].isoformat(),
                    "resource_ids": list(w["resource_ids"]),
                }
                self.queue.enqueue(job)
                flushed.append(job)
                del self.open_windows[key]
        return flushed


class WebhookHandler(object):
    """The logic behind the notification endpoint.

    handle(query_params, body_text) -> (status_code, response_body_text)

    Covers: the validation handshake, clientState verification, lifecycle
    notifications (reauthorizationRequired, subscriptionRemoved), and normal
    change notifications fanned into the batcher."""

    def __init__(self, subscription_manager, batcher):
        self.subs = subscription_manager
        self.batcher = batcher
        self.dropped_bad_state = 0

    def handle(self, query_params, body_text):
        # 1. Validation handshake: echo the token as text/plain within 10s.
        token = query_params.get("validationToken")
        if token is not None:
            return 200, token

        try:
            payload = json.loads(body_text) if body_text else {}
        except ValueError:
            return 400, "malformed body"

        for note in payload.get("value", []):
            sub_id = note.get("subscriptionId")
            rec = self.subs.store.get(sub_id)
            if rec is None or note.get("clientState") != rec["client_state"]:
                self.dropped_bad_state += 1
                continue

            lifecycle = note.get("lifecycleEvent")
            if lifecycle == "reauthorizationRequired":
                self.subs.reauthorize(sub_id)
                continue
            if lifecycle == "subscriptionRemoved":
                self.subs.store.pop(sub_id, None)
                # recreate on the next ensure_* timer pass
                continue
            if lifecycle is not None:
                continue

            resource = note.get("resource", "")
            resource_data = note.get("resourceData", {}) or {}
            resource_id = resource_data.get("id") or resource
            source_ref = _container_ref(rec, resource)
            self.batcher.add(rec["source_type"], source_ref, resource_id)

        # Graph requires a fast 202 for notification posts.
        return 202, ""


def _container_ref(sub_record, resource_path):
    """Derive the container (team/channel, chat, mailbox) from the resource
    path so the batcher windows by conversation, not by tenant firehose."""
    if sub_record["source_type"] == "email":
        return sub_record["user_ref"]
    # e.g. teams('..id..')/channels('..id..')/messages('..id..')
    #      chats('..id..')/messages('..id..')
    parts = resource_path.split("/messages")[0] if resource_path else ""
    return parts or sub_record["resource"]
