"""Tests for the cadence tick's inbox handling.

No database, no Ollama: the store and the model are injected, so what is
under test is the retry policy itself -- which window gets retried, which
gets set aside, and which is never counted against.

The case that motivated this: a window the model cannot parse fails every
tick forever, and a file waiting in the inbox is indistinguishable from
the ordinary wait for Ollama.

Run: python3 test_cadence.py
"""

import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join("..", "extraction"))
import cadence_tick as ct

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


WINDOW = {
    "job": {"source_type": "meeting_transcript", "source_ref": "w-1",
            "window_start": "2026-01-05T10:00:00Z",
            "window_end": "2026-01-05T10:30:00Z"},
    "directory": {"Robin Vale": "aad-robin"},
    "items": [{"resource_id": "r-1", "author_name": "Robin Vale",
               "author_ref": "aad-robin",
               "timestamp": "2026-01-05T10:00:00Z",
               "text": "invented line"}],
}


def fake_store(inserted=1):
    """Stand in for signal_store_pg without a database."""
    mod = types.ModuleType("signal_store_pg")
    mod.write_batch = lambda dsn, batch, fallback: {
        "signals_inserted": inserted, "pool_events": 0, "already_present": 0,
        "routed_to_author": 0, "unrouted": 0}
    return mod


def setup(tmp, name="window-1.json"):
    path = os.path.join(tmp, name)
    with open(path, "w") as f:
        json.dump(WINDOW, f)
    return path


def always_fails(model, host):
    def call(messages):
        raise RuntimeError("timed out")
    return call


def always_works(model, host):
    def call(messages):
        return json.dumps([{
            "type": "status_update", "title": "Do the invented thing",
            "speaker": "Robin Vale", "owner_candidates": [],
            "status_signal": "in_progress", "deadline": None,
            "blocker": None, "confidence": 0.8, "evidence_items": [1]}])
    return call


def run(tmp, llm_factory, ollama=True):
    """ollama_up is patched deliberately: with it left real, every test
    here takes the unreachable-model path and passes without extracting
    anything -- which is how the first draft of this file "passed"."""
    real = ct.ollama_up
    ct.ollama_up = lambda host, timeout=3: ollama
    try:
        return ct.process_inbox("dsn://none", tmp, "http://fake",
                                "fake-model", llm_factory)
    finally:
        ct.ollama_up = real


def test_quarantine_after_repeated_failure():
    print("quarantine")
    tmp = tempfile.mkdtemp()
    try:
        setup(tmp)
        for attempt in (1, 2):
            run(tmp, always_fails)
            ok(os.path.exists(os.path.join(tmp, "window-1.json")),
               "attempt {0} leaves the window in place to retry".format(
                   attempt))
        counts = json.load(open(os.path.join(tmp, ct.ATTEMPTS_FILE)))
        ok(counts["window-1.json"]["count"] == 2,
           "failures are counted per file across ticks")

        run(tmp, always_fails)
        ok(not os.path.exists(os.path.join(tmp, "window-1.json")),
           "the third failure takes it out of the inbox")
        q = os.path.join(tmp, "quarantine", "window-1.json")
        ok(os.path.exists(q),
           "it is moved to quarantine/, not deleted -- the data survives")
        note = open(q + ".error.txt").read()
        ok("timed out" in note and "NOT" in note,
           "the note carries the real error and says nothing was processed")
        ok("Move the .json back" in note,
           "and states the recovery procedure, which is the whole point")
        ok("window-1.json" not in json.load(
            open(os.path.join(tmp, ct.ATTEMPTS_FILE))),
           "the counter is cleared once the file is quarantined")
    finally:
        shutil.rmtree(tmp)


def test_success_clears_history():
    print("recovery")
    tmp = tempfile.mkdtemp()
    try:
        setup(tmp)
        sys.modules["signal_store_pg"] = fake_store()
        run(tmp, always_fails)
        ok(json.load(open(os.path.join(tmp, ct.ATTEMPTS_FILE)))[
               "window-1.json"]["count"] == 1,
           "one failure recorded")
        total = run(tmp, always_works)
        ok(total == 1 and os.path.exists(
            os.path.join(tmp, "processed", "window-1.json")),
           "a later success processes it normally")
        ok(json.load(open(os.path.join(tmp, ct.ATTEMPTS_FILE))) == {},
           "success clears the count, so a flaky window never accumulates "
           "its way into quarantine")
    finally:
        sys.modules.pop("signal_store_pg", None)
        shutil.rmtree(tmp)


def test_ollama_down_is_not_a_failure():
    print("degraded mode")
    tmp = tempfile.mkdtemp()
    try:
        setup(tmp)
        for _ in range(5):
            run(tmp, always_fails, ollama=False)
        ok(os.path.exists(os.path.join(tmp, "window-1.json")),
           "an unreachable model leaves every window untouched")
        ok(not os.path.exists(os.path.join(tmp, ct.ATTEMPTS_FILE)),
           "and counts nothing against them -- five ticks with Ollama down "
           "must not quarantine anything")
    finally:
        shutil.rmtree(tmp)


def test_corrupt_counter_is_survivable():
    print("corrupt counter")
    tmp = tempfile.mkdtemp()
    try:
        setup(tmp)
        with open(os.path.join(tmp, ct.ATTEMPTS_FILE), "w") as f:
            f.write("{not json at all")
        ok(ct._attempts_load(tmp) == {},
           "a corrupt counter reads as empty rather than raising")
        run(tmp, always_fails)
        ok(os.path.exists(os.path.join(tmp, "window-1.json")),
           "the tick still runs; losing the count costs retries, not the day")
    finally:
        shutil.rmtree(tmp)


def test_one_bad_window_does_not_block_others():
    print("isolation")
    tmp = tempfile.mkdtemp()
    try:
        setup(tmp, "window-bad.json")
        setup(tmp, "window-good.json")
        sys.modules["signal_store_pg"] = fake_store()
        run(tmp, always_works)
        ok(os.path.exists(os.path.join(tmp, "processed", "window-bad.json"))
           and os.path.exists(os.path.join(tmp, "processed",
                                           "window-good.json")),
           "every window in a tick is processed, not just the first")
    finally:
        sys.modules.pop("signal_store_pg", None)
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_quarantine_after_repeated_failure()
    test_success_clears_history()
    test_ollama_down_is_not_a_failure()
    test_corrupt_counter_is_survivable()
    test_one_bad_window_does_not_block_others()
    print("\nall {0} cadence checks passed".format(CHECKS[0]))
