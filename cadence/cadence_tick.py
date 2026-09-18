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
  TL_JUDGE_MODEL    default: best installed chat model, announced; none -> hold
  TL_EXTRACT_MODEL  default: best installed chat model, announced
  OLLAMA_HOST_URL   default http://127.0.0.1:11434
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
import time
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


def installed_models(host, timeout=5):
    """The models Ollama can actually serve, or None if it cannot say."""
    try:
        with urllib.request.urlopen(host + "/api/tags",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:                                   # noqa: BLE001
        return None


def missing_models(host, wanted):
    """Which of the wanted models are not installed. [] when all are, or
    when Ollama cannot be asked -- an unreachable server is the offline
    path's business, not this check's."""
    tags = installed_models(host)
    if tags is None:
        return []
    def ok(m):
        return any(t == m or t.startswith(m + ":")
                   or (":" not in m and t.split(":")[0] == m)
                   for t in tags)
    return sorted({m for m in wanted if m and not ok(m)})


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

# A window is retried until it works, which is right for the ordinary
# failure (Ollama down, database asleep) and wrong for a window that can
# never succeed. One real window drove the model to 15k characters of
# malformed JSON and 480-680s against a 300s socket timeout: it failed
# every tick, forever, and "a file waiting in the inbox" is exactly what
# healthy degraded mode looks like. So failures are counted per file and a
# window that keeps failing is set aside where a person can see it.
QUARANTINE_AFTER = 3
ATTEMPTS_FILE = ".attempts.json"


def _attempts_load(inbox):
    """Best-effort. A corrupt counter must never stop the tick -- losing
    the count costs a few extra retries; refusing to run costs the day."""
    try:
        with open(os.path.join(inbox, ATTEMPTS_FILE)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (IOError, ValueError):
        return {}


def _attempts_save(inbox, attempts):
    try:
        with open(os.path.join(inbox, ATTEMPTS_FILE), "w") as f:
            json.dump(attempts, f, indent=1, sort_keys=True)
    except IOError:
        pass


def quarantine(inbox, path, record):
    """Set a repeatedly-failing window aside, with why, beside it.

    Not deleted and not silently skipped: moving it back into the inbox is
    the whole recovery procedure, and the note says what to expect.
    """
    qdir = os.path.join(inbox, "quarantine")
    os.makedirs(qdir, exist_ok=True)
    name = os.path.basename(path)
    shutil.move(path, os.path.join(qdir, name))
    try:
        with open(os.path.join(qdir, name + ".error.txt"), "w") as f:
            f.write(
                "This window failed extraction {0} times and was set aside "
                "so it would stop\nconsuming every tick. It was NOT "
                "processed and no signals from it exist.\n\n"
                "last attempt: {1}\nlast error:   {2}\n\n"
                "Move the .json back into the inbox to retry it.\n".format(
                    record.get("count"), record.get("last_at"),
                    record.get("last_error")))
    except IOError:
        pass


def acquire_tick_lock(dsn):
    """One tick at a time, enforced by the database itself.

    There is now an hourly scheduled tick AND a human who runs ticks by
    hand, so collision is not hypothetical -- it is the default. Two ticks
    race shutil.move on the same inbox file; the loser's exception is
    recorded as a failed ATTEMPT, so a scheduling accident walks an
    innocent window toward quarantine. And both pay full model price for
    the same extraction first.

    A Postgres advisory lock instead of a lock file because a lock file
    has the one failure mode that matters here: a killed tick leaves it
    behind, and the machine this runs on gets killed a lot. An advisory
    lock dies with its connection -- crash, Ctrl+C, reboot, all release
    it. The connection is returned so the caller can keep it alive for
    the duration; None means another tick holds it.
    """
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext('task-ledger-tick'))")
        if cur.fetchone()[0]:
            return conn
        # Losing the race used to be reported without saying WHO won it,
        # which turns a correct guard into a guessing game: is the other
        # tick real (wait) or a zombie session (kill it)? The database
        # knows; ask it and hand the answer to the person.
        cur.execute("""
            SELECT a.pid, a.state,
                   to_char(a.backend_start, 'YYYY-MM-DD HH24:MI')
                     AS since,
                   COALESCE(a.application_name, '') AS app
            FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory'
              AND l.objid = hashtext('task-ledger-tick')::bigint
                            & x'FFFFFFFF'::bigint
              AND l.granted""")
        holder = cur.fetchone()
    conn.close()
    return ("held", holder) if holder else None


def _dsn_label(dsn):
    """The host and port from a DSN, without leaking a password into a log."""
    bits = [p for p in (dsn or "").split()
            if p.startswith(("host=", "port=", "dbname="))]
    return " ".join(bits) or "the configured DSN"


def db_up(dsn, timeout=3):
    """Is the store there to write to."""
    import psycopg2
    try:
        psycopg2.connect(dsn, connect_timeout=timeout).close()
        return True
    except Exception:                                   # noqa: BLE001
        return False


def process_inbox(dsn, inbox, host, extract_model, llm_factory):
    import extraction_worker
    import signal_store_pg

    files = sorted(glob.glob(os.path.join(inbox, "*.json")))
    if not files:
        log("inbox: empty")
        return 0
    if not ollama_up(host):
        # Deliberately before any counting: an unreachable model is the
        # normal degraded path and must never push a window toward
        # quarantine.
        log("inbox: {0} file(s) waiting but Ollama unreachable; "
            "leaving for next tick".format(len(files)))
        return 0
    if not db_up(dsn):
        # The same courtesy the model already got, for the dependency that
        # did not have it. Extraction runs first and the store is only
        # touched at the end, so a database that went away mid-run was
        # discovered AFTER the expensive part: on a laptop that slept
        # through a tick, fifteen minutes of local inference produced a
        # window that could not be written, was counted as a failed
        # attempt, and moved one strike closer to quarantine. Nothing about
        # that is the window's fault, and none of the work needed doing.
        log("inbox: {0} file(s) waiting but the database is unreachable; "
            "leaving for next tick (extraction would be discarded at the "
            "write and counted against the window)".format(len(files)))
        return 0
    processed_dir = os.path.join(inbox, "processed")
    os.makedirs(processed_dir, exist_ok=True)
    attempts = _attempts_load(inbox)
    quarantined = 0
    total = 0
    for path in files:
        name = os.path.basename(path)
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
            attempts.pop(name, None)          # a success clears the history
            shutil.move(path, os.path.join(processed_dir, name))
        except Exception as e:
            record = attempts.get(name) or {"count": 0}
            record["count"] = record.get("count", 0) + 1
            record["last_error"] = str(e)[:300]
            record["last_at"] = datetime.datetime.now().isoformat(
                timespec="seconds")
            attempts[name] = record
            if record["count"] >= QUARANTINE_AFTER:
                quarantine(inbox, path, record)
                attempts.pop(name, None)
                quarantined += 1
                log("inbox: QUARANTINED {0} after {1} failed attempts: {2} "
                    "-- moved to quarantine/, see the .error.txt beside "
                    "it".format(name, record["count"], e))
            else:
                log("inbox: FAILED {0} (attempt {1} of {2}): {3} -- left in "
                    "place".format(name, record["count"], QUARANTINE_AFTER,
                                   e))
    _attempts_save(inbox, attempts)
    if quarantined:
        log("inbox: {0} window(s) quarantined this tick and will NOT be "
            "retried".format(quarantined))
    return total


def pooled_users(dsn, primary):
    """Everyone with signals waiting, not just whoever owns this tick.

    One meeting names eight people and pools claims for all of them. A tick
    that reconciles only TL_USER leaves the other seven with claims that sit
    unconsumed indefinitely -- on the first real corpus, seven colleagues'
    worth of work never reached a packet at all. The primary user is always
    included so a quiet day still supersedes and still notifies.

    Standing candidates count as work waiting too, and for a while they did
    not. A candidate is re-assessed only on a run that reconciles its owner,
    and an owner with an empty pool was never reconciled -- so once somebody
    consumed their last signal, their register froze. Relaxing the promotion
    bar then appeared to do nothing for them no matter how many times the
    tick ran, which is the same failure d338a89 fixed inside the reconciler
    reappearing one level up, in who the reconciler is called for. On the
    first corpus that hid 9 candidates across 3 people, including the only
    one standing at the new bar.
    """
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_ref FROM v_unconsumed_signals")
            users = {r[0] for r in cur.fetchall()} | {primary}
            try:
                cur.execute("""
                    SELECT DISTINCT user_ref FROM v_task_candidates
                    WHERE state = 'sighted'""")
                users |= {r[0] for r in cur.fetchall()}
            except psycopg2.errors.UndefinedTable:
                conn.rollback()   # register not migrated yet; signals only
    finally:
        conn.close()
    return sorted(users)


def run_reconcile(dsn, user, host, embed_model, judge_model):
    import board_io_pg
    import embedders
    import reconciler as rec_mod

    signals = board_io_pg.load_signals(dsn, user)
    tasks, version = board_io_pg.load_board(dsn, user)
    all_tasks = board_io_pg.load_all_boards(dsn)
    candidates = board_io_pg.load_candidates(
        dsn, user,
        window_days=(rec_mod.load_policy().get("candidate_promotion")
                     or {}).get("window_days"))
    log("reconcile[{0}]: {1} pooled signals, {2} matchable tasks, {3} tasks "
        "across all boards, {4} candidate(s), board v{5}".format(
            user, len(signals), len(tasks), len(all_tasks),
            len(candidates), version))

    if ollama_up(host):
        # Cached because an embedding is a pure function and these texts
        # barely change between ticks; the wrapper degrades to the plain
        # embedder on any cache trouble.
        embed = embedders.CachedEmbedder(
            embedders.OllamaEmbedder(embed_model, host), dsn, embed_model)
        # judge_model may be None on a machine with no chat model
        # installed: real embeddings, no judge, judge-band held.
        judge = ollama_chat(judge_model, host) if judge_model else None
        mode = "ollama" + ("" if judge_model else " (no judge: holds)")
    else:
        embed = embedders.TokenEmbedder()
        judge = None
        mode = "offline (holds judge-band)"
    log("reconcile: mode={0}".format(mode))

    run_id = "cadence-{0}".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    rec = rec_mod.Reconciler(embed=embed, judge_llm=judge)
    packet, report = rec.reconcile(user, signals, tasks, version, run_id,
                                   all_tasks=all_tasks,
                                   candidates=candidates)
    result = board_io_pg.write_packet(dsn, packet, report["consumed"],
                                     report["annotations"], run_id,
                                     report=report)
    if getattr(embed, "calls_saved", 0):
        log("reconcile[{0}]: embedding cache saved {1} model call(s)"
            .format(user, embed.calls_saved))
    log("reconcile[{0}]: {1} proposals, {2} consumed, {3} held, "
        "{4} candidate sighting(s), {5} promoted, superseded {6}".format(
            user, result["proposals"], len(report["consumed"]),
            len(report["held"]), len(report["candidate_sightings"]),
            len(report["candidate_promotions"]), len(result["superseded"])))
    # A judge that could not be reached held signals that a working one
    # might have linked. Silent, that reads as a quiet week; said, it is
    # one line pointing at the model.
    if report.get("judge_failures"):
        log("  judge[{0}]: {1} of {2} call(s) FAILED ({3}) -- those "
            "signals were held, not guessed at. A slow local model is "
            "the usual cause; TL_JUDGE_TIMEOUT is the budget."
            .format(user, report["judge_failures"], report["judge_calls"],
                    ", ".join("{0} x{1}".format(k, n) for k, n in sorted(
                        (report.get("judge_failure_kinds") or {}).items()))))
    # A promotion count of zero has three quite different causes and they
    # are worth telling apart without a psql session.
    for h in (report.get("held_detail") or [])[:8]:
        log("    held: {0} \u201c{1}\u201d -- {2}".format(
            h["signal_type"], h["title"][:48],
            "closest \u201c{0}\u201d at {1:.2f}".format(
                h["closest"][:40], h["similarity"])
            if h.get("closest") and h.get("similarity") is not None
            else "no plausible task on the board"))
    a = report.get("candidate_assessment") or {}
    if a.get("assessed"):
        log("  candidates[{0}]: {1} assessed against {2}; {3} short, "
            "{4} uncitable, {5} promoted (best seen: {6} sighting(s), "
            "{7} speaker(s))".format(
                user, a["assessed"], a["bar"], a["short"], a["uncitable"],
                a["promoted"], a["best_sightings"], a["best_speakers"]))
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
    host = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
    inbox = os.path.expanduser(
        os.environ.get("TL_INBOX", "~/task-ledger-inbox"))
    os.makedirs(inbox, exist_ok=True)

    wire_paths()
    log("tick start: user={0} inbox={1}".format(user, inbox))
    # Nothing below this line works without the store, so ask once, here.
    # The check existed already but only guarded extraction, so an empty
    # inbox skipped it and a stopped Postgres surfaced as an unhandled
    # OperationalError several minutes into the reconcile -- three times in
    # one day, each time reading as a crash rather than as a service that
    # is not running. Portable Postgres is not a Windows service and does
    # not come back by itself, so this is the ordinary case, not the
    # exceptional one, and it deserves one readable line.
    if not db_up(dsn):
        log("FATAL: database unreachable at {0} -- nothing can be read or "
            "written. Start it (deploy/windows/pg_start.bat on Windows) "
            "and run again.".format(_dsn_label(dsn)))
        return 2
    lock = acquire_tick_lock(dsn)
    if lock is None or isinstance(lock, tuple):
        holder = lock[1] if isinstance(lock, tuple) else None
        # --steal: terminate the holder's session and take the lock.
        # Offered BY the tick rather than as pasted psql because every
        # rescue command this project has handed out lately assumed
        # something about the machine -- PATH, hostname resolution,
        # wmic existing -- and each assumption cost a round trip. This
        # one runs inside the connection that already works. And it is
        # safe BY THE SYSTEM'S OWN DESIGN, which is the only reason it
        # exists: ticks are idempotent over append-only storage, a
        # killed tick's lock dies with its session, and the next tick
        # resumes from exactly what is recorded. Worst case, a real
        # tick dies mid-window and this run redoes that window.
        if "--steal" in sys.argv and holder:
            log("stealing the tick lock: terminating session {0} "
                "(idempotent storage makes this safe; a real tick "
                "killed here just means this run redoes its "
                "in-flight window)".format(holder[0]))
            import psycopg2
            c = psycopg2.connect(dsn)
            c.autocommit = True
            with c.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)",
                            (holder[0],))
            c.close()
            time.sleep(1.0)
            lock = acquire_tick_lock(dsn)
            if lock is None or isinstance(lock, tuple):
                log("  still held (a scheduler may have restarted one "
                    "instantly) -- giving up politely.")
                return 0
        else:
            log("another tick already holds the lock -- this one bows "
                "out; nothing was started twice.")
            if holder:
                pid, state, since, app = holder
                log("  the holder: database session {0} ({1}), "
                    "connected since {2}{3}.".format(
                        pid, state or "?", since,
                        " via " + app if app else ""))
                log("  A real tick is a running cadence_tick.py "
                    "process; between model calls its session shows "
                    "'idle', so idle alone proves nothing.")
                log("  Sure nothing real is running (or happy to let "
                    "this run redo its window)? Take the lock:")
                log("    tl tick --steal")
            else:
                log("  (could not identify the holder -- likely "
                    "released in the last instant; just run this "
                    "again)")
            return 0
    # A configured model that is not installed does not fail fast on its
    # own: extraction calls time out per window, half an hour each, and
    # the run "completes" having learned nothing. It happened twice in one
    # week -- a paste from one machine naming a model only the other one
    # has. Ollama being DOWN is not this check's business (the offline
    # path holds work safely); a reachable Ollama that lacks the named
    # model is a configuration error and gets one line, immediately.
    # UNSET model envs are resolved against what this Ollama actually
    # has, in a stated preference order -- never against a hard-coded
    # name. The old default, mistral-small3.1, existed on the machine
    # this was written on and never on the machine it ran on, so every
    # fresh window FATALed until somebody remembered the right `set`
    # line: the exact invisible-window-state disease this launcher has
    # been curing all week, living in its own preflight. An EXPLICITLY
    # set model that is missing still fails fast -- a person who named
    # a model meant that model, and silently substituting would make
    # the run a lie. Auto-picks are announced, with how to pin.
    have = installed_models(host)
    if have is None:
        # Unreachable is NOT "no models installed" -- it is nobody home,
        # and it has its own long-standing answer: the offline pass.
        # The auto-pick change briefly collapsed the two and turned a
        # stopped Ollama into a FATAL that told the operator to pull a
        # model he already had. Extraction leaves the inbox alone,
        # reconciliation runs the offline matcher and holds ambiguity,
        # and this line says the one thing worth doing about it.
        log("Ollama is not answering at {0} -- start it (Windows key, "
            "type Ollama, Enter; look for the tray icon). Running this "
            "tick OFFLINE: transcripts wait in the inbox, matching uses "
            "the offline embedder, ambiguity holds.".format(host))
        log("models: extract=offline judge=offline embed=offline")
        return _run_stages(dsn, user, host, inbox)
    if have == []:
        log("FATAL: Ollama is running but has NO models installed. "
            "Pull the two it needs:")
        log("    ollama pull qwen2.5:7b")
        log("    ollama pull nomic-embed-text")
        return 2

    def _pick(env, preferred, purpose):
        val = os.environ.get(env)
        if val:
            return val, val
        for cand in preferred:
            hit = [m for m in have
                   if m == cand or m.split(":")[0] == cand.split(":")[0]]
            if hit:
                log("{0} unset -- using installed {1} for {2} "
                    "(set {0} to pin a choice)".format(
                        env, hit[0], purpose))
                return hit[0], hit[0] + "(auto)"
        return None, None

    extract_model, extract_label = _pick(
        "TL_EXTRACT_MODEL",
        ["mistral-small3.1", "qwen2.5:7b", "llama3.1:8b"], "extraction")
    judge_model, judge_label = _pick(
        "TL_JUDGE_MODEL",
        ["mistral-small3.1", "llama3.1:8b", "qwen2.5:7b"], "judging")
    embed_model = os.environ.get("TL_EMBED_MODEL", "nomic-embed-text")
    if extract_model is None:
        log("FATAL: no usable extraction model. Installed: {0}. Pull "
            "one (ollama pull qwen2.5:7b) or set TL_EXTRACT_MODEL."
            .format(", ".join(have) or "none"))
        return 2
    if judge_model is None:
        log("judge: none installed -- ambiguous matches will be HELD, "
            "not guessed at. Pull a chat model to enable judging.")
        judge_label = "none"
    missing = missing_models(host, [
        m for m in (extract_model, judge_model, embed_model) if m])
    if missing:
        log("FATAL: model(s) not installed on this Ollama: {0}. "
            "Installed: {1}. Set TL_*_MODEL to one of those or pull the "
            "missing one.".format(", ".join(missing),
                                  ", ".join(have) or "none"))
        return 2
    # Make the resolved choices authoritative for every stage this tick
    # spawns -- subprocesses read the same envs.
    os.environ["TL_EXTRACT_MODEL"] = extract_model
    if judge_model:
        os.environ["TL_JUDGE_MODEL"] = judge_model
    else:
        os.environ.pop("TL_JUDGE_MODEL", None)
    # Echo the models actually in force. A pasted `set` line that never
    # reached the program is otherwise indistinguishable from the run
    # intended.
    log("models: extract={0} judge={1} embed={2}".format(
        extract_label, judge_label, embed_model))
    return _run_stages(dsn, user, host, inbox)


def _run_stages(dsn, user, host, inbox):
    """Everything after preflight: extraction, reconciliation, poke,
    digest. Runs identically whether models were resolved or the tick
    is offline -- each stage already carries its own degraded path
    (extraction leaves the inbox when Ollama is unreachable; the
    reconciler falls back to the offline embedder and holds)."""
    # And the promotion bar in force, read from the same file the reconciler
    # will read. An edit that never saved -- a mangled paste, a findstr that
    # hung before it could confirm, a Notepad dialog nobody clicked -- is
    # otherwise invisible: the tick runs, reports 0 promoted, and looks
    # exactly like a policy change that was applied and did not work.
    try:
        import reconciler as _rec
        _cp = _rec.load_policy().get("candidate_promotion") or {}
        log("promotion: mode={0} min_sightings={1} min_distinct_speakers={2} "
            "window_days={3}".format(
                _cp.get("mode"), _cp.get("min_sightings"),
                _cp.get("min_distinct_speakers"), _cp.get("window_days")))
    except Exception as exc:                            # noqa: BLE001
        log("promotion: policy unreadable ({0!r})".format(exc))
    try:
        process_inbox(dsn, inbox, host,
                      os.environ.get("TL_EXTRACT_MODEL", "offline"),
                      ollama_chat)
        # Everyone with claims waiting, not just this tick's owner. The
        # notification below stays about TL_USER -- one person is being
        # poked -- but the other boards get their packets built rather than
        # their claims ageing in the pool until somebody runs the CLI.
        everyone = pooled_users(dsn, user)
        if len(everyone) > 1:
            log("reconcile: {0} user(s) have signals waiting".format(
                len(everyone)))
        for who in everyone:
            try:
                run_reconcile(dsn, who, host,
                              os.environ.get("TL_EMBED_MODEL",
                                             "nomic-embed-text"),
                              os.environ.get("TL_JUDGE_MODEL"))
            except Exception as exc:                    # noqa: BLE001
                # One person's packet failing must not cost everyone else
                # theirs; the tick reports it and carries on.
                log("reconcile[{0}] FAILED: {1!r}".format(who, exc))
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
