"""Live Graph webhook host: notifications in, cadence-inbox files out.

The whole point of this module is WHERE it lands: flushed windows are
written as {job, directory, items} JSON into the cadence inbox -- the seam
every downstream stage already trusts. The live integration doesn't get
its own pipeline; it feeds the proven one.

Flow:
  Graph change notification (mail, this proof's clean path)
    -> clientState verified (reject spoofs, same rule as the bot host)
    -> message fetched by id via app token (notifications carry pointers,
       not content -- matching the evidence philosophy)
    -> WindowBatcher (the real one from graph_ingestion) groups bursts
    -> flush writes an inbox file; the cadence tick or run_local extracts

Run:
    python3 webhook_host.py --port 8443
    # then expose via a tunnel and create the subscription:
    #   devtunnel host -p 8443 --allow-anonymous     (or ngrok http 8443)
    #   python3 subscribe_mail.py create --url https://<tunnel>/webhook

Endpoints:
  POST /webhook   validation handshake + notifications
  GET  /healthz   liveness + batcher state

Python 3.8 compatible. Requires flask (already a project dep).
"""

import argparse
import datetime
import json
import os
import re
import sys
import threading
import time

from flask import Flask, Response, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ingestion"))
sys.path.insert(0, HERE)

import graph_auth              # noqa: E402
from graph_ingestion import WindowBatcher  # noqa: E402

INBOX = os.path.expanduser(os.environ.get("TL_INBOX", "~/task-ledger-inbox"))
STATE_PATH = os.path.join(graph_auth.CONF_DIR, "subscriptions.json")


def load_client_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return {s["id"]: s["client_state"]
                    for s in json.load(f).get("subscriptions", [])}
    return {}


def ref_for(address, display):
    """Deterministic person_ref for a home tenant: aad-<mailbox local>."""
    local = (address or display or "unknown").split("@")[0].lower()
    return "aad-" + re.sub(r"[^a-z0-9]+", "-", local).strip("-")


class InboxQueue(object):
    """WindowBatcher's queue target: a flushed window becomes an inbox
    file, with items fetched fresh by resource id at flush time."""

    def __init__(self, fetch_item):
        self.fetch_item = fetch_item
        self.written = []

    def enqueue(self, job):
        items, directory = [], {}
        for rid in job["resource_ids"]:
            item = self.fetch_item(rid)
            if item is None:
                continue
            directory[item["author_name"]] = item["author_ref"]
            items.append(item)
        if not items:
            return
        spec = {
            "job": {"source_type": job["source_type"],
                    "source_ref": job["source_ref"],
                    "window_start": job["window_start"],
                    "window_end": job["window_end"]},
            "directory": directory,
            "items": items,
        }
        os.makedirs(INBOX, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_ref = re.sub(r"[^A-Za-z0-9_-]+", "_", job["source_ref"])[:60]
        path = os.path.join(INBOX, "live-{0}-{1}.json".format(stamp,
                                                              safe_ref))
        with open(path, "w") as f:
            json.dump(spec, f, indent=1)
        self.written.append(path)
        print("[webhook] window flushed -> {0} ({1} items)".format(
            path, len(items)))


def fetch_mail_item(resource_path):
    """resource_path like Users/{id}/Messages/{id} from the notification."""
    token = graph_auth.app_token()
    msg = graph_auth.graph_get("/" + resource_path.lstrip("/"), token)
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    body = msg.get("bodyPreview") or ""
    subject = msg.get("subject") or ""
    return {
        "resource_id": msg["id"][-24:],
        "author_name": sender.get("name") or sender.get("address")
        or "unknown",
        "author_ref": ref_for(sender.get("address"), sender.get("name")),
        "timestamp": msg.get("receivedDateTime")
        or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "text": (subject + "\n" + body).strip(),
    }


def make_app(client_states=None, fetch_item=fetch_mail_item, batcher=None):
    app = Flask(__name__)
    queue = InboxQueue(fetch_item)
    b = batcher or WindowBatcher(queue, quiet_seconds=60,
                                 max_window_minutes=15)
    states = client_states if client_states is not None \
        else load_client_state()
    app.config["batcher"] = b
    app.config["queue"] = queue

    @app.route("/webhook", methods=["POST", "GET"])
    def webhook():
        token = request.args.get("validationToken")
        if token:
            # Graph's handshake: echo within 10s, text/plain.
            return Response(token, mimetype="text/plain")
        payload = request.get_json(silent=True) or {}
        accepted = 0
        for note in payload.get("value", []):
            sub_id = note.get("subscriptionId", "")
            expect = states.get(sub_id)
            if expect is None or note.get("clientState") != expect:
                print("[webhook] REJECTED notification: unknown "
                      "subscription or bad clientState")
                continue
            resource = note.get("resource", "")
            source_ref = "mailbox-" + sub_id[:8]
            b.add("email_thread", source_ref, resource)
            accepted += 1
        # Fast 202 regardless: Graph retries slow endpoints punitively.
        return ("", 202) if accepted or payload else ("", 202)

    @app.route("/healthz")
    def healthz():
        return jsonify({"open_windows": len(b.open_windows),
                        "written": len(queue.written)})

    return app


def flusher_loop(app, interval=15):
    while True:
        time.sleep(interval)
        try:
            app.config["batcher"].flush_due()
        except Exception as e:
            print("[webhook] flush error: {0!r}".format(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    args = ap.parse_args()
    app = make_app()
    t = threading.Thread(target=flusher_loop, args=(app,), daemon=True)
    t.start()
    print("[webhook] inbox: {0}".format(INBOX))
    print("[webhook] known subscriptions: {0}".format(
        len(load_client_state())))
    app.run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
