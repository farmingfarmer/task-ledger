#!/usr/bin/env python3
"""Which person is seeing which ledger, under TL_BLIND=1.

    python3 scripts/blind_map.py alex priya vic devesh

The participants are blind; you are not, and you have to be -- an answer
you cannot attribute to a ledger is worth nothing. Every survey file
records its own mapping, so this is for BEFORE the session: check the
split, and if it is lopsided, change TL_BLIND_SALT and look again.

Assignment is a hash of (salt, person), which is stable per person and
unbiased in expectation -- not balanced. With five people a 4/1 split is
ordinary luck, and 4/1 is worth re-salting away from: it means four
people met the same ledger first and any first-impression effect lands
almost entirely on one arm.
"""
import argparse
import hashlib
import os
import sys

SALT = os.environ.get("TL_BLIND_SALT", "task-ledger-blind-v1")
ARMS = ("real", "copilot")


def arm_of(person, salt=SALT):
    """Same computation as dashboard_api.blind_map, deliberately duplicated:
    this must be runnable without Flask, psycopg2 or a live database."""
    digest = hashlib.sha256(
        (salt + "\x1f" + person).encode("utf-8")).hexdigest()
    pair = list(reversed(ARMS)) if int(digest[:8], 16) % 2 else list(ARMS)
    return {"arm-a": pair[0], "arm-b": pair[1]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("people", nargs="+",
                    help="names as the picker shows them, with or without "
                         "the aad- prefix")
    ap.add_argument("--salt", default=SALT)
    ap.add_argument("--balance", action="store_true",
                    help="search for a salt that splits THESE people "
                         "evenly, and print the line that sets it")
    args = ap.parse_args()

    refs = [n if n.startswith("aad-") else "aad-" + n for n in args.people]
    if args.balance:
        # Hashing is unbiased in expectation and says nothing about any
        # particular five people: the shipped salt happens to put all
        # five of one team on the same side, which is 1-in-32 and still
        # happened. Rather than explain that to somebody mid-session,
        # find a salt that splits this list and hand it over.
        want = len(refs) // 2
        for i in range(20000):
            cand = "{0}-{1}".format(SALT, i)
            real = sum(1 for r in refs if arm_of(r, cand)["arm-a"] == "real")
            if real == want or real == len(refs) - want:
                print("salt that splits these {0} people {1}/{2}:\n"
                      .format(len(refs), real, len(refs) - real))
                print('   set "TL_BLIND_SALT={0}"'.format(cand))
                print("\nSet it before starting the dashboard, and use the "
                      "SAME one for everybody -- changing it mid-session "
                      "swaps which ledger a person is looking at.\n")
                args.salt = cand
                break
        else:
            print("no balanced salt found in 20000 tries, which should be "
                  "impossible; using the one given.\n")

    print("salt: {0}\n".format(args.salt))
    print("{0:<28} {1:<14} {2}".format("person", "Ledger A is", "Ledger B is"))
    tally = {"real": 0, "copilot": 0}
    for name in args.people:
        ref = name if name.startswith("aad-") else "aad-" + name
        m = arm_of(ref, args.salt)
        tally[m["arm-a"]] += 1
        print("{0:<28} {1:<14} {2}".format(ref, m["arm-a"], m["arm-b"]))
    n = len(args.people)
    print("\nLedger A was the real ledger for {0} of {1}, Copilot's for "
          "{2}.".format(tally["real"], n, tally["copilot"]))
    if n >= 3 and min(tally.values()) * 3 < n:
        print("That is lopsided: most people meet the same ledger first, so "
              "any first-impression effect lands on one arm. Try another "
              "TL_BLIND_SALT and run this again until the split is even, "
              "then set that salt for the session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
