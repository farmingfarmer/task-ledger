"""Task Ledger cadence tick.

What one tick does, in order:
  1. INBOX    every *.json in TL_INBOX (run_local transcript format: job +
              directory + items) is extracted via Ollama and written to the
              signal store, then moved to TL_INBOX/processed/. If Ollama is
              unreachable, inbox files are left untouched for the next tick
              -- extraction needs a model; nothing is lost by waiting.
  2. RECONCILE  the user's pool against their board. Ollama up: real
              embeddings + judge. Ollama down: offline TokenEmbedder and no
              judge -- auto-links and gates still run, judge-band and
              closure signals hold. write_packet supersedes any open packet
              (storage-layer semantics) and skips empty packets.
  3. DIGEST   on TL_DIGEST_DAY, the week's digest is written as HTML.
              A quiet week writes nothing; a failure here never fails the
              tick, because the digest only summarises work already safely
              recorded.
  4. NOTIFY   if any undecided proposals exist for the user (new packet or
              a still-open one), raise a notification pointing at the
              review CLI -- osascript on macOS, the no-admin PowerShell
              toast on Windows, the log everywhere else. Zero pending ->
              zero noise.

Everything is logged with timestamps to stdout (launchd redirects to the
log file). Exit code 0 unless the tick itself failed; a down Ollama or an
empty day is normal operation, not failure.

Configuration (environment, set in the launchd plist or a shell):
  DB_DSN            required
  TL_USER           required (e.g. aad-dev)
  TL_REPO           repo root (default: two levels up from this file)
  TL_INBOX          transcript inbox (default ~/task-ledger-inbox)
  TL_EMBED_MODEL    default nomic-embed-text
  TL_JUDGE_MODEL    default mistral-small3.1
  TL_EXTRACT_MODEL  default mistral-small3.1
  OLLAMA_HOST_URL   default http://localhost:11434
  TL_NO_NOTIFY      set to 1 to suppress the popup (tests, headless)
  TL_DIGEST_DAY     weekday for the weekly digest (0=Mon, default 4=Fri)
  TL_DIGEST_DIR     where digests are written (default ~/task-ledger-digests)

Python 3.8 compatible. Requires psycopg2, pyyaml, jsonschema.
"""

import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.request


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{0}] {1}".format(ts, msg))
    sys.stdout.flush()


def repo_root():
    return os.environ.get(
        "TL_REPO",
        os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..")))


def wire_paths():
    root = repo_root()
    for sub in ("extraction", "reconciler", "policy", "digest"):
        p = os.path.join(root, sub)
        if p not in sys.path:
            sys.path.insert(0, p)


def ollama_up(host, timeout=3):
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def ollama_chat(model, host):
    def call(messages):
        body = json.dumps({"model": model, "messages": messages,
                           "stream": False,
                           "options": {"temperature": 0.0,
                                       "num_ctx": 16384}}).encode("utf-8")
        req = urllib.request.Request(
            host + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode("utf-8"))["message"]["content"]
    return call


def notify(title, text):
    """Pop the platform's own notification. macOS: osascript. Windows: the
    no-admin toast from deploy/windows, run through PowerShell with the
    execution policy bypassed for this one process (the port's whole
    premise is a locked-down machine where nothing may be installed or
    configured). Anywhere else, and whenever the toast script is missing,
    the line still lands in the tick log -- a notification that cannot be
    shown must never fail the tick."""
    if os.environ.get("TL_NO_NOTIFY") == "1":
        log("notify suppressed: {0} -- {1}".format(title, text))
        return
    if sys.platform == "darwin":
        script = ('display notification "{0}" with title "{1}" '
                  'sound name "Glass"').format(text.replace('"', "'"),
                                               title.replace('"', "'"))
        subprocess.run(["osascript", "-e", script], check=False)
        return
    if sys.platform == "win32":
        toast = os.path.join(repo_root(), "deploy", "windows",
                             "windows_toast.ps1")
        if os.path.exists(toast):
            log("notify (toast): {0} -- {1}".format(title, text))
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass",
                            "-File", toast, "-Text",
                            "{0}: {1}".format(title, text)], check=False)
            return
        log("notify (toast script missing at {0}): {1} -- {2}".format(
            toast, title, text))
        return
    log("notify ({0}): {1} -- {2}".format(sys.platform, title, text))


# ------------------------------------------------------------------- stages

def process_inbox(dsn, inbox, host, extract_model, llm_factory):
    import extraction_worker
    import signal_store_pg

    files = sorted(glob.glob(os.path.join(inbox, "*.json")))
    if not files:
        log("inbox: empty")
        return 0
    if not ollama_up(host):
        log("inbox: {0} file(s) waiting but Ollama unreachable; "
            "leaving for next tick".format(len(files)))
        return 0
    processed_dir = os.path.join(inbox, "processed")
    os.makedirs(processed_dir, exist_ok=True)
    total = 0
    for path in files:
        try:
            with open(path) as f:
                spec = json.load(f)
            worker = extraction_worker.ExtractionWorker(
                fetcher=lambda job, items=spec["items"]: items,
                llm=llm_factory(extract_model, host),
                directory=spec["directory"],
                model_name="ollama:{0}".format(extract_model))
            batch, report = worker.run_job(spec["job"])
            fallback = sorted(set(spec["directory"].values()))
            result = signal_store_pg.write_batch(dsn, batch, fallback)
            log("inbox: {0} -> {1} signals kept ({2} dropped), "
                "{3} inserted".format(
                    os.path.basename(path), report["kept"],
                    len(report["dropped"]), result["signals_inserted"]))
            # Routing is the difference between one review card and ten.
            if result.get("routed_to_author") or result.get("unrouted"):
                log("    routing: {0} unowned claim(s) went to their "
                    "speaker, {1} could not be routed at all".format(
                        result.get("routed_to_author", 0),
                        result.get("unrouted", 0)))
            for d in report["dropped"]:
                log("    dropped [{0}]: {1}".format(
                    d.get("reason"), (d.get("title") or "")[:70]))
            total += result["signals_inserted"]
            shutil.move(path, os.path.join(processed_dir,
                                           os.path.basename(path)))
        except Exception as e:
            log("inbox: FAILED {0}: {1} -- left in place".format(
                os.path.basename(path), e))
    return total


def run_reconcile(dsn, user, host, embed_model, judge_model):
    import board_io_pg
    import embedders
    import reconciler as rec_mod

    signals = board_io_pg.load_signals(dsn, user)
    tasks, version = board_io_pg.load_board(dsn, user)
    all_tasks = board_io_pg.load_all_boards(dsn)
    log("reconcile: {0} pooled signals, {1} matchable tasks, {2} tasks "
        "across all boards, board v{3}".format(
            len(signals), len(tasks), len(all_tasks), version))

    if ollama_up(host):
        embed = embedders.OllamaEmbedder(embed_model, host)
        judge = ollama_chat(judge_model, host)
        mode = "ollama"
    else:
        embed = embedders.TokenEmbedder()
        judge = None
        mode = "offline (holds judge-band)"
    log("reconcile: mode={0}".format(mode))

    run_id = "cadence-{0}".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    rec = rec_mod.Reconciler(embed=embed, judge_llm=judge)
    packet, report = rec.reconcile(user, signals, tasks, version, run_id,
                                   all_tasks=all_tasks)
    result = board_io_pg.write_packet(dsn, packet, report["consumed"],
                                     report["annotations"], run_id)
    log("reconcile: {0} proposals, {1} consumed, {2} held, "
        "superseded {3}, {4} signals returned".format(
            result["proposals"], len(report["consumed"]),
            len(report["held"]), len(result["superseded"]),
            result["returned_signals"]))
    return result


def weekly_digest(dsn, user, out_dir):
    """On the digest day, write the week's digest and say where it went.

    The tick already knows the ledger's state; a digest is that state
    addressed to a person. It writes a file rather than sending anything,
    because a cadence job that can mail cannot be run casually -- delivery
    belongs to the bot host, and the file is what a human or a mail step
    picks up. A quiet week writes nothing at all.
    """
    import digest as digest_mod
    import digest_io_pg

    data = digest_io_pg.load_personal(dsn, user, days=7)
    d = digest_mod.build_personal(user, data)
    if d["quiet"]:
        log("digest: quiet week for {0}; nothing written".format(user))
        return None
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    path = os.path.join(out_dir, "digest-{0}-{1}.html".format(user, stamp))
    with open(path, "w") as f:
        f.write(digest_mod.render_html(d))
    log("digest: {0} -> {1}".format(d["headline"], path))
    return d


def pending_count(dsn, user):
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                  SELECT DISTINCT ON (packet_id) packet_id, state
                  FROM packet_state_events
                  ORDER BY packet_id, event_id DESC)
                SELECT count(p.proposal_id)
                FROM review_packets rp
                JOIN latest l USING (packet_id)
                JOIN proposals p USING (packet_id)
                LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
                WHERE rp.user_ref = %s
                  AND l.state IN ('draft','delivered','partially_decided')
                  AND d.decision_id IS NULL""", (user,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def main():
    dsn = os.environ.get("DB_DSN")
    user = os.environ.get("TL_USER")
    if not dsn or not user:
        log("FATAL: DB_DSN and TL_USER are required")
        return 2
    host = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
    inbox = os.path.expanduser(
        os.environ.get("TL_INBOX", "~/task-ledger-inbox"))
    os.makedirs(inbox, exist_ok=True)

    wire_paths()
    log("tick start: user={0} inbox={1}".format(user, inbox))
    try:
        process_inbox(dsn, inbox, host,
                      os.environ.get("TL_EXTRACT_MODEL", "mistral-small3.1"),
                      ollama_chat)
        run_reconcile(dsn, user, host,
                      os.environ.get("TL_EMBED_MODEL", "nomic-embed-text"),
                      os.environ.get("TL_JUDGE_MODEL", "mistral-small3.1"))
        n = pending_count(dsn, user)
        if n > 0:
            notify("Task Ledger",
                   "{0} proposal{1} awaiting your review".format(
                       n, "" if n == 1 else "s"))
            log("pending: {0} -> notified".format(n))
        else:
            log("pending: 0 -> silent")

        # Weekly digest, on one configured weekday (Friday by default).
        # Same discipline as the notification: only when there is
        # something to say.
        digest_day = int(os.environ.get("TL_DIGEST_DAY", "4"))
        if datetime.datetime.now().weekday() == digest_day:
            try:
                d = weekly_digest(dsn, user, os.path.expanduser(
                    os.environ.get("TL_DIGEST_DIR", "~/task-ledger-digests")))
                if d and n == 0:
                    notify("Task Ledger", d["headline"])
            except Exception as e:
                # A digest is a summary of work already safely recorded;
                # failing to write one must not fail the tick.
                log("digest FAILED (tick continues): {0!r}".format(e))
        log("tick done")
        return 0
    except Exception as e:
        log("tick FAILED: {0!r}".format(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
