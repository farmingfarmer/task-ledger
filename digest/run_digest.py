"""Build and deliver a digest.

    python3 digest/run_digest.py --as aad-dev --dsn "$DB_DSN"
    python3 digest/run_digest.py --as aad-dev --dsn "..." --format html \
        --out ~/digest.html
    python3 digest/run_digest.py --project proj-drift --dsn "..." \
        --format text --to-recipients      # who currently holds a grant
    python3 digest/run_digest.py --as aad-dev --dsn "..." --format card

Delivery is deliberately unopinionated. The digest renders to text, HTML,
or an Adaptive Card payload, and goes to stdout or a file; the bot host
delivers the card into Teams the same way it delivers a review card, and
anything else (mail, a static page, a webhook) can consume the text or the
HTML without this script knowing about it. Nothing here sends mail, so
nothing here can leak a digest to the wrong address.

A quiet week prints nothing and exits 0 unless --even-if-quiet: a digest
that arrives every week whether or not it has anything to say is a digest
people filter into a folder.

Python 3.8 compatible. Requires psycopg2.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import digest as digest_mod          # noqa: E402
import digest_io_pg                  # noqa: E402


def render(d, fmt):
    if fmt == "text":
        return digest_mod.render_text(d)
    if fmt == "html":
        return digest_mod.render_html(d)
    return json.dumps(digest_mod.build_digest_card(d), indent=2)


def main():
    ap = argparse.ArgumentParser(description="Build a Task Ledger digest.")
    ap.add_argument("--as", dest="user", help="personal digest for this ref")
    ap.add_argument("--project", help="project digest for this project_id")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--format", choices=["text", "html", "card"],
                    default="text")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--even-if-quiet", action="store_true",
                    help="send even when there is nothing to report")
    ap.add_argument("--to-recipients", action="store_true",
                    help="list who currently holds a grant on the project")
    args = ap.parse_args()

    if bool(args.user) == bool(args.project):
        raise SystemExit("choose exactly one of --as or --project")

    if args.user:
        data = digest_io_pg.load_personal(args.dsn, args.user, args.days)
        d = digest_mod.build_personal(args.user, data)
    else:
        project, data = digest_io_pg.load_project(args.dsn, args.project,
                                                  args.days)
        if project is None:
            raise SystemExit("no such project: " + args.project)
        d = digest_mod.build_project(project, data)
        if args.to_recipients:
            for r in digest_io_pg.recipients(args.dsn, args.project):
                sys.stderr.write("recipient: {0} ({1}{2})\n".format(
                    r["grantee_ref"], r["level"],
                    ", until " + r["expires_at"].date().isoformat()
                    if r.get("expires_at") else ""))

    if d["quiet"] and not args.even_if_quiet:
        sys.stderr.write("quiet week for {0}: nothing to send\n".format(
            d["audience"]))
        return 0

    text = render(d, args.format)
    if args.out:
        with open(os.path.expanduser(args.out), "w") as f:
            f.write(text)
        sys.stderr.write("wrote {0}\n".format(args.out))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
