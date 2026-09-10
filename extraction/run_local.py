"""Run the extraction worker locally against Ollama or Azure OpenAI.

Usage (Ollama, e.g. on the Mac):
    python3 run_local.py --transcript sample_transcript.json
    python3 run_local.py --transcript sample_transcript.json \
        --model mistral-small3.1 --write-db "host=localhost port=5433 dbname=taskintel user=postgres"

Usage (Azure OpenAI):
    set AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_KEY / AZURE_OPENAI_DEPLOYMENT
    python3 run_local.py --transcript sample_transcript.json --backend azure

The transcript file supplies the window, the directory (display name ->
person ref), and the items. Output: the validated ExtractionBatch as JSON to
stdout plus the salvage report to stderr; --write-db additionally persists it
through signal_store_pg.

Python 3.8 compatible. Stdlib only (urllib for HTTP).
"""

import argparse
import json
import os
import sys
import urllib.request

import extraction_worker


def ollama_llm(model, host="http://127.0.0.1:11434"):
    def call(messages):
        body = json.dumps({
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode("utf-8")
        req = urllib.request.Request(
            host + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["message"]["content"]
    return call


def azure_llm():
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    key = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    url = ("{0}/openai/deployments/{1}/chat/completions"
           "?api-version=2024-10-21").format(endpoint, deployment)

    def call(messages):
        body = json.dumps({"messages": messages,
                           "temperature": 0.1}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "api-key": key})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    return call


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--backend", choices=["ollama", "azure"],
                    default="ollama")
    ap.add_argument("--model", default="mistral-small3.1")
    ap.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    ap.add_argument("--write-db", default=None,
                    help="DSN; if set, persist via signal_store_pg")
    args = ap.parse_args()

    with open(args.transcript) as f:
        spec = json.load(f)

    if args.backend == "ollama":
        llm = ollama_llm(args.model, args.ollama_host)
        model_name = "ollama:{0}".format(args.model)
    else:
        llm = azure_llm()
        model_name = "azure-openai:{0}".format(
            os.environ.get("AZURE_OPENAI_DEPLOYMENT", "unknown"))

    worker = extraction_worker.ExtractionWorker(
        fetcher=lambda job: spec["items"],
        llm=llm,
        directory=spec["directory"],
        model_name=model_name)

    batch, report = worker.run_job(spec["job"])
    sys.stderr.write("report: " + json.dumps(report, indent=2) + "\n")
    print(json.dumps(batch, indent=2))

    if args.write_db:
        import signal_store_pg
        fallback = sorted(set(spec["directory"].values()))
        result = signal_store_pg.write_batch(args.write_db, batch, fallback)
        sys.stderr.write("db write: " + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
