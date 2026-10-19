#!/usr/bin/env python3
"""Audit every built kit before it is sent to a person.

    python scripts\\kit_audit.py            # static checks, all kits
    python scripts\\kit_audit.py --boot     # also cold-boot each kit
                                            # and verify it over HTTP

A kit is a promise made to a colleague's double-click. This audits the
promise per kit, not per build: files that must exist, files that must
NOT exist, the config that personalises it, the dump that must carry no
role statements, the zip that must actually open, and -- with --boot --
the launcher walked end to end with the dashboard answering for the
right person, one ledger, no blind.

Exit 0 only when every kit passes everything.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile

HOME = os.path.expanduser("~")
PORT = 8090

REQUIRED = ["START-TASK-LEDGER.bat", "REFRESH-LEDGER.bat", "ledger.sql",
            "kit-config.json", "if-something-goes-wrong.md"]
# A single-mechanism kit must not carry the pilot's comparison gear,
# and build/run debris must never ride along to a colleague.
FORBIDDEN_NAMES = ["ledger-b.sql", "blind-salt.txt"]
FORBIDDEN_GLOBS = ["*.log", "*status*.txt", "copilot_claims*",
                   "*_raw.txt", "my-answers"]


def fail(kit, what):
    print("  FAIL  {0}: {1}".format(os.path.basename(kit), what))
    return 1


def ok(what):
    print("  ok    " + what)
    return 0


def audit_static(kit, expect_return):
    bad = 0
    for name in REQUIRED:
        if not os.path.exists(os.path.join(kit, name)):
            bad += fail(kit, "missing required file: " + name)
    for name in FORBIDDEN_NAMES:
        if os.path.exists(os.path.join(kit, name)):
            bad += fail(kit, "forbidden file present: " + name)
    for pat in FORBIDDEN_GLOBS:
        hits = glob.glob(os.path.join(kit, pat))
        if hits:
            bad += fail(kit, "debris matching {0}: {1}".format(
                pat, ", ".join(os.path.basename(h) for h in hits)))

    cfgp = os.path.join(kit, "kit-config.json")
    cfg = {}
    if os.path.exists(cfgp):
        with open(cfgp, encoding="utf-8") as f:
            cfg = json.load(f)
        want = os.path.basename(kit).replace("task-ledger-kit-", "")
        if cfg.get("for", "").replace("aad-", "") != want:
            bad += fail(kit, "kit-config 'for'={0!r} does not name the "
                             "folder's person {1!r}".format(
                                 cfg.get("for"), want))
        if cfg.get("ledger") != "copilot":
            bad += fail(kit, "kit-config ledger={0!r}, expected "
                             "'copilot'".format(cfg.get("ledger")))
        if expect_return and expect_return not in (cfg.get("return_to")
                                                   or ""):
            bad += fail(kit, "return_to={0!r} lacks {1!r}".format(
                cfg.get("return_to"), expect_return))

    ledp = os.path.join(kit, "ledger.sql")
    digest = None
    if os.path.exists(ledp):
        with open(ledp, "rb") as f:
            blob = f.read()
        if len(blob) < 100_000:
            bad += fail(kit, "ledger.sql suspiciously small "
                             "({0} bytes)".format(len(blob)))
        if re.search(rb"OWNER TO", blob):
            bad += fail(kit, "ledger.sql contains OWNER TO -- dump was "
                             "not taken with --no-owner")
        # pg_dump emits COPY rows in whatever order the scan returns
        # them, so byte-hashing flags identical data as different (it
        # did, eight ways). Hash the SORTED lines: same multiset of
        # lines means the same dump, whatever order it was written in.
        # ...and pg_dump 17 stamps each dump with a RANDOM \restrict
        # guard token (meta-command injection protection), so those two
        # lines differ by design. Same data, same digest, nothing else.
        digest = hashlib.sha256(b"\n".join(sorted(
            ln for ln in blob.splitlines()
            if not ln.startswith((b"\\restrict", b"\\unrestrict"))
        ))).hexdigest()

    # The launcher must be the single-mechanism one.
    lp = os.path.join(kit, "START-TASK-LEDGER.bat")
    if os.path.exists(lp):
        with open(lp, encoding="utf-8", errors="replace") as f:
            launcher = f.read()
        if "TL_DISABLE_COPILOT_PROBE" not in launcher:
            bad += fail(kit, "launcher predates the single-mechanism "
                             "fix -- rebuild this kit")

    zp = kit + ".zip"
    if not os.path.exists(zp):
        bad += fail(kit, "no send-ready zip beside the folder")
    else:
        try:
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
                base = os.path.basename(kit)
                if not any(n.replace("\\", "/") ==
                           base + "/START-TASK-LEDGER.bat" for n in names):
                    bad += fail(kit, "zip does not carry the launcher at "
                                     "<kit>/START-TASK-LEDGER.bat")
                if z.testzip() is not None:
                    bad += fail(kit, "zip is corrupt (testzip)")
        except zipfile.BadZipFile:
            bad += fail(kit, "zip does not open at all")
    return bad, digest, cfg


def http_json(path, timeout=5, viewer=None):
    req = urllib.request.Request(
        "http://127.0.0.1:{0}{1}".format(PORT, path),
        headers={"X-Viewer": viewer} if viewer else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def port_busy():
    try:
        http_json("/api/version", timeout=2)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def audit_boot(kit, cfg):
    """Cold-boot THIS kit's launcher, verify over HTTP, shut it down."""
    if port_busy():
        return fail(kit, "port {0} already serving -- close the running "
                         "Task Ledger first".format(PORT))
    env = dict(os.environ, TL_KIT_NO_BROWSER="1")
    proc = subprocess.Popen(
        ["cmd", "/c", os.path.join(kit, "START-TASK-LEDGER.bat")],
        cwd=kit, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    bad = 0
    ver = None
    try:
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                ver = http_json("/api/version")
                break
            except Exception:                           # noqa: BLE001
                if proc.poll() is not None:
                    break
                time.sleep(3)
        if ver is None:
            bad += fail(kit, "dashboard never answered on {0} "
                             "(launcher exit: {1})".format(
                                 PORT, proc.poll()))
        else:
            if ver.get("blind"):
                bad += fail(kit, "BLIND is on in a single-mechanism kit")
            if ver.get("ledgers") not in (["real"], None):
                bad += fail(kit, "serves {0!r}, expected one ledger"
                            .format(ver.get("ledgers")))
            k = ver.get("kit") or {}
            if k.get("for") != cfg.get("for"):
                bad += fail(kit, "greets {0!r}, config says {1!r}".format(
                    k.get("for"), cfg.get("for")))
            try:
                today = http_json("/api/today", viewer=cfg.get("for"))
                if not isinstance(today, dict):
                    bad += fail(kit, "/api/today gave no counts")
            except Exception as exc:                    # noqa: BLE001
                bad += fail(kit, "/api/today errored: {0}".format(exc))
            if not bad:
                ok("boots, greets {0}, one ledger, blind off, today "
                   "strip answers".format(k.get("for_name") or
                                          k.get("for")))
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while port_busy() and time.time() < deadline:
            time.sleep(2)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", action="store_true",
                    help="also cold-boot every kit and verify over HTTP")
    ap.add_argument("--expect-return", default="med.usc.edu",
                    help="substring every kit's return address must carry")
    ap.add_argument("--kits", default=os.path.join(
        HOME, "task-ledger-kit-*"))
    args = ap.parse_args()

    kits = sorted(d for d in glob.glob(args.kits)
                  if os.path.isdir(d)
                  and os.path.exists(os.path.join(d, "kit-config.json")))
    if not kits:
        print("no kits found under " + args.kits)
        return 2

    total_bad = 0
    digests = {}
    for kit in kits:
        print(os.path.basename(kit))
        bad, digest, cfg = audit_static(kit, args.expect_return)
        if digest:
            digests.setdefault(digest, []).append(os.path.basename(kit))
        if not bad:
            ok("static: files, config, dump, zip")
        total_bad += bad
        if args.boot and os.path.exists(
                os.path.join(kit, "START-TASK-LEDGER.bat")):
            total_bad += audit_boot(kit, cfg)

    # Every kit must carry the SAME ledger -- a kit with a different
    # dump got built against different data and nobody would know.
    if len(digests) > 1:
        total_bad += 1
        print("FAIL: kits carry {0} different ledger dumps:".format(
            len(digests)))
        for d, names in digests.items():
            print("   {0}...: {1}".format(d[:12], ", ".join(names)))
    elif digests:
        ok("all kits carry the identical ledger dump")

    print("\n{0}: {1} kit(s), {2} problem(s)".format(
        "AIRTIGHT" if total_bad == 0 else "NOT READY",
        len(kits), total_bad))
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
