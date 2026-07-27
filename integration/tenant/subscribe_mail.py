"""Manage the live mail subscription: the clean no-protected-API path.

    python3 subscribe_mail.py create --url https://<tunnel>/webhook \
        --user you@yourtenant.onmicrosoft.com
    python3 subscribe_mail.py list
    python3 subscribe_mail.py renew
    python3 subscribe_mail.py delete

Creates a Graph change-notification subscription on the user's messages
(app permission Mail.Read, admin-consented -- no protected-API approval
needed, unlike Teams message subscriptions). A random clientState is
minted per subscription and stored in ~/.task-ledger/subscriptions.json;
the webhook host rejects any notification that doesn't present it --
identical spoof posture to the bot host's decided_by rule.

Mail subscriptions max out around 3 days; `renew` extends whatever is
stored. The half-life renewal pattern from SubscriptionManager applies in
production; for a proof, renewing when you sit down is fine.
Python 3.8 compatible.
"""

import argparse
import datetime
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_auth  # noqa: E402

STATE_PATH = os.path.join(graph_auth.CONF_DIR, "subscriptions.json")


def _load():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"subscriptions": []}


def _save(state):
    os.makedirs(graph_auth.CONF_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1)


def _expiry(hours=70):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=hours)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def create(args):
    token = graph_auth.app_token()
    client_state = secrets.token_urlsafe(24)
    body = {
        "changeType": "created",
        "notificationUrl": args.url,
        "resource": "/users/{0}/messages".format(args.user),
        "expirationDateTime": _expiry(),
        "clientState": client_state,
    }
    sub = graph_auth.graph_post("/subscriptions", token, body)
    state = _load()
    state["subscriptions"].append({
        "id": sub["id"], "resource": sub["resource"],
        "client_state": client_state,
        "expires": sub["expirationDateTime"]})
    _save(state)
    print("created subscription {0}".format(sub["id"]))
    print("  resource: {0}".format(sub["resource"]))
    print("  expires:  {0}".format(sub["expirationDateTime"]))
    print("clientState stored; the webhook host will now accept it.")


def list_(args):
    token = graph_auth.app_token()
    live = graph_auth.graph_get("/subscriptions", token)
    known = {s["id"] for s in _load()["subscriptions"]}
    if not live:
        print("no live subscriptions")
    for s in live:
        print("{0}  {1}  expires {2}{3}".format(
            s["id"][:12], s["resource"], s["expirationDateTime"],
            "" if s["id"] in known else "   [NOT OURS / no clientState]"))


def renew(args):
    token = graph_auth.app_token()
    state = _load()
    for s in state["subscriptions"]:
        try:
            graph_auth.graph_post("/subscriptions/" + s["id"] + "?", token,
                                  {})
        except RuntimeError:
            pass
        # PATCH via urllib: reuse graph_post shape with method override is
        # clumsy; do it directly.
        import urllib.request
        req = urllib.request.Request(
            graph_auth.GRAPH + "/subscriptions/" + s["id"],
            data=json.dumps({"expirationDateTime": _expiry()}).encode(),
            method="PATCH",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            new = json.loads(resp.read().decode())
        s["expires"] = new["expirationDateTime"]
        print("renewed {0} -> {1}".format(s["id"][:12], s["expires"]))
    _save(state)


def delete(args):
    token = graph_auth.app_token()
    state = _load()
    for s in state["subscriptions"]:
        try:
            code = graph_auth.graph_delete("/subscriptions/" + s["id"],
                                           token)
            print("deleted {0} ({1})".format(s["id"][:12], code))
        except Exception as e:
            print("delete {0} failed: {1}".format(s["id"][:12], e))
    state["subscriptions"] = []
    _save(state)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--url", required=True)
    c.add_argument("--user", required=True)
    sub.add_parser("list")
    sub.add_parser("renew")
    sub.add_parser("delete")
    args = ap.parse_args()
    {"create": create, "list": list_, "renew": renew,
     "delete": delete}[args.cmd](args)


if __name__ == "__main__":
    main()
