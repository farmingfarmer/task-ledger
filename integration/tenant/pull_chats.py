"""Pull YOUR Teams chats into inbox windows -- no protected APIs.

App-only subscriptions to Teams messages sit behind Microsoft's
protected-API approval even on your own tenant. Delegated access to your
OWN chats (Chat.Read, you sign in) does not. This tool proves the Teams
content path -- real chat messages flowing through the real extraction
seam -- while the protected-API request form does its weeks of waiting.

    python3 pull_chats.py --hours 24            # windows from recent chats
    python3 pull_chats.py --hours 24 --list     # just show chats

First run triggers the device-code sign-in (a URL and a short code);
after that the refresh token carries you. Each active chat becomes one
inbox window. Python 3.8 compatible.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_auth  # noqa: E402

INBOX = os.path.expanduser(os.environ.get("TL_INBOX", "~/task-ledger-inbox"))


def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def ref_for(display):
    return "aad-" + re.sub(r"[^a-z0-9]+", "-",
                           (display or "unknown").lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    token = graph_auth.user_token()
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=args.hours))

    chats = graph_auth.graph_get("/me/chats", token,
                                 {"$top": 50})
    print("{0} chats visible".format(len(chats)))
    written = 0
    for chat in chats:
        topic = chat.get("topic") or chat.get("chatType") or "chat"
        if args.list:
            print("  {0}  {1}".format(chat["id"][:20], topic))
            continue
        msgs = graph_auth.graph_get(
            "/me/chats/{0}/messages".format(chat["id"]), token,
            {"$top": 50})
        items, directory = [], {}
        for m in sorted(msgs, key=lambda m: m.get("createdDateTime", "")):
            created = m.get("createdDateTime", "")
            if not created or created < since.strftime("%Y-%m-%dT%H:%M:%SZ"):
                continue
            frm = ((m.get("from") or {}).get("user") or {})
            name = frm.get("displayName")
            body = strip_html((m.get("body") or {}).get("content"))
            if not name or not body:
                continue   # system events, cards, empty bodies
            directory[name] = ref_for(name)
            items.append({
                "resource_id": m["id"][-20:],
                "author_name": name,
                "author_ref": ref_for(name),
                "timestamp": created,
                "text": body,
            })
        if not items:
            continue
        spec = {
            "job": {"source_type": "teams_chat",
                    "source_ref": "chat-" + chat["id"][:16],
                    "window_start": items[0]["timestamp"],
                    "window_end": items[-1]["timestamp"]},
            "directory": directory,
            "items": items,
        }
        os.makedirs(INBOX, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", topic)[:40]
        path = os.path.join(INBOX, "chat-{0}.json".format(safe or "dm"))
        with open(path, "w") as f:
            json.dump(spec, f, indent=1)
        print("  {0}: {1} messages -> {2}".format(topic, len(items), path))
        written += 1
    if not args.list:
        print("{0} window(s) in {1} -- the cadence tick (or run_local) "
              "extracts them".format(written, INBOX))


if __name__ == "__main__":
    main()
