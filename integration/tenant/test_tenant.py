"""Offline tests for the tenant webhook host. Run: python3 test_tenant.py
Validates: Graph validation handshake, clientState spoof rejection,
notification batching, inbox-file emission in the exact cadence spec, and
end-to-end compatibility with the real extraction worker. No network."""
import json, os, sys, shutil, datetime

os.environ["TL_INBOX"] = "/tmp/tenant-inbox-test"
shutil.rmtree("/tmp/tenant-inbox-test", ignore_errors=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webhook_host

checks = [0]
def ok(c, label):
    assert c, label
    checks[0] += 1
    print("  pass:", label)

def fake_fetch(resource_path):
    return {"resource_id": resource_path[-12:],
            "author_name": "Alex Marunycz",
            "author_ref": "aad-alexander-marunycz",
            "timestamp": "2026-07-24T18:00:00Z",
            "text": "Subject line\nI finished the tenant webhook proof."}

def main():
    app = webhook_host.make_app(
        client_states={"sub-123": "expected-state"},
        fetch_item=fake_fetch)
    client = app.test_client()

    r = client.post("/webhook?validationToken=abc%20xyz")
    ok(r.status_code == 200 and r.data == b"abc xyz"
       and r.mimetype == "text/plain",
       "validation handshake echoes token as text/plain")

    bad = {"value": [{"subscriptionId": "sub-123", "clientState": "WRONG",
                      "resource": "Users/u1/Messages/m1"}]}
    r = client.post("/webhook", json=bad)
    ok(r.status_code == 202
       and len(app.config["batcher"].open_windows) == 0,
       "bad clientState: 202 fast-ack but nothing batched")

    good = {"value": [{"subscriptionId": "sub-123",
                       "clientState": "expected-state",
                       "resource": "Users/u1/Messages/m1"},
                      {"subscriptionId": "sub-123",
                       "clientState": "expected-state",
                       "resource": "Users/u1/Messages/m2"}]}
    r = client.post("/webhook", json=good)
    ok(r.status_code == 202
       and len(app.config["batcher"].open_windows) == 1,
       "valid notifications batch into one open window")

    b = app.config["batcher"]
    w = b.open_windows[list(b.open_windows)[0]]
    w["last_at"] = w["last_at"] - datetime.timedelta(seconds=999)
    b.flush_due()
    inbox = os.environ["TL_INBOX"]
    files = os.listdir(inbox)
    ok(len(files) == 1 and files[0].startswith("live-"),
       "flushed window wrote one inbox file")
    spec = json.load(open(os.path.join(inbox, files[0])))
    ok(set(spec) == {"job", "directory", "items"}
       and spec["job"]["source_type"] == "email_thread"
       and len(spec["items"]) == 2
       and spec["items"][0]["author_ref"] == "aad-alexander-marunycz",
       "inbox file matches the cadence spec exactly (2 items, refs mapped)")

    sys.path.insert(0, os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "..", "..", "extraction"))
    import extraction_worker
    resp = json.dumps({"signals": [{
        "type": "completion_claim", "title": "Tenant webhook proof",
        "description": None,
        "owner_candidates": [{"name": "Alex Marunycz",
                              "basis": "self_reference",
                              "confidence": 0.9}],
        "status_signal": "completed", "deadline": None, "blocker": None,
        "confidence": 0.9, "evidence_items": ["1"]}]})
    wk = extraction_worker.ExtractionWorker(
        fetcher=lambda job: spec["items"], llm=lambda m: resp,
        directory=spec["directory"], model_name="canned:t")
    batch, report = wk.run_job(spec["job"])
    ok(report["kept"] == 1 and batch["signals"][0]["evidence"][0]
       ["source_type"] == "email_message",
       "live-shaped window flows through the real worker end to end")

    r = client.get("/healthz")
    ok(r.get_json()["written"] == 1, "healthz reports batcher state")

    print("\nall {0} tenant webhook checks passed".format(checks[0]))

if __name__ == "__main__":
    main()
