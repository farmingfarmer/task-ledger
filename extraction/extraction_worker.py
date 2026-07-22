"""Extraction worker: communication window -> schema-valid ExtractionBatch.

Pipeline for one job (the unit emitted by the WindowBatcher or the transcript
poll plan):

  fetch content items (injected fetcher; Graph in prod, fixtures in tests)
   -> build prompt (extraction_prompts.build_messages)
   -> call LLM (injected callable; Azure OpenAI or Ollama)
   -> parse + SALVAGE: validate each signal individually, keep the good,
      drop the malformed with recorded reasons (one bad signal never sinks
      a batch)
   -> resolve display names to person refs via the injected directory
      (schema forbids display names; unmappable candidates are dropped)
   -> convert cited item numbers to EvidenceRefs: Graph resource id +
      sha256 content hash of the item text at extraction time
   -> assemble ExtractionBatch with DETERMINISTIC ids (content hashes), so
      re-running a window is an idempotent no-op downstream
   -> validate the whole batch against extraction_schema.json before
      returning; an invalid batch raises rather than propagating.

The worker never touches the board and never stores anything itself --
persistence is signal_store_pg.write_batch(). Python 3.8 compatible.
Requires jsonschema.
"""

import datetime
import hashlib
import json
import os
import re

import jsonschema

import extraction_prompts

PROMPT_VERSION = "extract-v1.1"

VALID_TYPES = {
    "task_mention", "assignment", "status_update", "completion_claim",
    "deadline_signal", "blocker", "blocker_resolved", "proposal",
    "cancellation",
}
VALID_STATUS = {"proposed", "in_progress", "blocked", "completed",
                "cancelled", None}
VALID_BASIS = {"explicit_assignment", "self_reference", "inferred"}
VALID_PRECISION = {"exact", "week", "month", "quarter", "vague"}

SOURCE_TO_EVIDENCE = {
    "meeting_transcript": "transcript_utterance",
    "teams_channel": "teams_message",
    "teams_chat": "teams_message",
    "email_thread": "email_message",
    "document": "document_span",
}


def _sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def find_schema_path(start_dir=None):
    """Locate extraction_schema.json relative to this module: works both in
    the repo layout (../contracts/) and a flat layout (../)."""
    base = start_dir or os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join("..", "contracts", "extraction_schema.json"),
                os.path.join("..", "extraction_schema.json"),
                "extraction_schema.json"):
        cand = os.path.normpath(os.path.join(base, rel))
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError("extraction_schema.json not found near " + base)


class ExtractionWorker(object):
    """One instance per model configuration.

    fetcher(job) -> list of items, each:
        {"resource_id": str, "author_name": str, "author_ref": str|None,
         "timestamp": iso str, "text": str}
    llm(messages) -> raw response text
    directory: {display_name (casefolded): person_ref} for owner resolution
    model_name: recorded in the batch for eval/regression
    """

    def __init__(self, fetcher, llm, directory, model_name,
                 schema_path=None, clock=None):
        self.fetcher = fetcher
        self.llm = llm
        self.directory = {k.casefold(): v for k, v in directory.items()}
        self.model_name = model_name
        path = schema_path or find_schema_path()
        with open(path) as f:
            self.schema = json.load(f)
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc))

    # ------------------------------------------------------------------ run

    def run_job(self, job):
        """job: {source_type, source_ref, window_start, window_end,
                 resource_ids?}  ->  (batch dict, report dict)

        The report carries what was dropped and why -- the worker's honesty
        channel for prompt tuning."""
        items = self.fetcher(job)
        report = {"items": len(items), "raw_signals": 0, "kept": 0,
                  "dropped": []}
        batch_id = _sha(job["source_ref"], job["window_start"],
                        job["window_end"], PROMPT_VERSION)
        batch = {
            "batch_id": batch_id,
            "extracted_at": self.clock().isoformat(),
            "extractor": {"model": self.model_name,
                          "prompt_version": PROMPT_VERSION},
            "window": {
                "source_type": job["source_type"],
                "source_ref": job["source_ref"],
                "time_start": job["window_start"],
                "time_end": job["window_end"],
            },
            "signals": [],
        }
        if not items:
            jsonschema.validate(batch, self.schema)
            return batch, report

        numbered = [dict(it, item_no=i + 1) for i, it in enumerate(items)]
        messages = extraction_prompts.build_messages(
            job["source_type"], job["window_end"][:10], numbered)
        raw = self.llm(messages)

        parsed, parse_err = _parse_json_array(raw)
        if parse_err:
            report["dropped"].append({"reason": "unparseable_response",
                                      "detail": parse_err})
            jsonschema.validate(batch, self.schema)
            return batch, report
        report["raw_signals"] = len(parsed)

        ev_type = SOURCE_TO_EVIDENCE[job["source_type"]]
        by_no = {it["item_no"]: it for it in numbered}
        seen_ids = set()
        for idx, sig in enumerate(parsed):
            built, why = self._build_signal(sig, idx, batch_id, ev_type, by_no)
            if built is None:
                report["dropped"].append({"index": idx, "reason": why,
                                          "title": _safe_title(sig)})
                continue
            if built["signal_id"] in seen_ids:
                report["dropped"].append({"index": idx, "reason": "duplicate",
                                          "title": built["title"]})
                continue
            seen_ids.add(built["signal_id"])
            batch["signals"].append(built)
            report["kept"] += 1

        jsonschema.validate(batch, self.schema)
        return batch, report

    # ------------------------------------------------------------- assembly

    def _build_signal(self, sig, idx, batch_id, ev_type, by_no):
        if not isinstance(sig, dict):
            return None, "not_an_object"
        stype = sig.get("type")
        if stype not in VALID_TYPES:
            return None, "bad_type:{0}".format(stype)
        title = (sig.get("title") or "").strip()
        if not title:
            return None, "missing_title"
        title = title[:120]

        conf = _num(sig.get("confidence"))
        if conf is None:
            conf = 0.6
        conf = max(0.0, min(1.0, conf))

        cited = sig.get("evidence_items") or []
        evidence = []
        for n in cited:
            it = by_no.get(n if isinstance(n, int) else _int_or_none(n))
            if it is None:
                continue
            evidence.append({
                "source_type": ev_type,
                "resource_id": it["resource_id"],
                "span": None,
                "content_hash": _sha(it["text"]),
            })
        if not evidence:
            return None, "no_valid_evidence"

        status = sig.get("status_signal")
        if status == "null":
            status = None
        if status not in VALID_STATUS:
            return None, "bad_status:{0}".format(status)

        deadline = self._clean_deadline(sig.get("deadline"))
        blocker = self._clean_blocker(sig.get("blocker"))
        owners = self._resolve_owners(sig.get("owner_candidates") or [])

        signal_id = _sha(batch_id, stype, evidence[0]["resource_id"], title)
        built = {
            "signal_id": signal_id,
            "type": stype,
            "title": title,
            "description": (sig.get("description") or None),
            "owner_candidates": owners,
            "status_signal": status,
            "deadline": deadline,
            "blocker": blocker,
            "confidence": round(conf, 3),
            "evidence": evidence,
        }
        # description key must be a string if present; drop None to keep
        # the object minimal and schema-clean.
        if built["description"] is None:
            del built["description"]
        return built, None

    def _resolve_owners(self, raw):
        out = []
        for cand in raw:
            if not isinstance(cand, dict):
                continue
            name = (cand.get("name") or "").strip().casefold()
            ref = self.directory.get(name)
            if ref is None:
                continue   # schema forbids display names; unmappable dropped
            basis = cand.get("basis")
            if basis not in VALID_BASIS:
                basis = "inferred"
            conf = _num(cand.get("confidence"))
            out.append({"person_ref": ref,
                        "confidence": max(0.0, min(1.0, conf if conf is not None else 0.5)),
                        "basis": basis})
        return out

    def _clean_deadline(self, d):
        if not isinstance(d, dict):
            return None
        raw_text = (d.get("raw_text") or "").strip()
        if not raw_text:
            return None
        precision = d.get("precision")
        if precision not in VALID_PRECISION:
            precision = "vague"
        date = d.get("date")
        if date is not None:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(date)):
                date = None   # never let a hallucinated date through
        return {"raw_text": raw_text, "precision": precision, "date": date}

    def _clean_blocker(self, b):
        if not isinstance(b, dict):
            return None
        desc = (b.get("description") or "").strip()
        if not desc:
            return None
        party = (b.get("blocking_party") or "").strip().casefold()
        return {"description": desc,
                "blocking_party_ref": self.directory.get(party)}


# ---------------------------------------------------------------- utilities

def _parse_json_array(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Tolerate leading/trailing prose by slicing to the outermost brackets.
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None, "no JSON array found"
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError as e:
        return None, "json error: {0}".format(e)
    if not isinstance(obj, list):
        return None, "top level is not an array"
    return obj, None


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_title(sig):
    if isinstance(sig, dict):
        return str(sig.get("title", ""))[:60]
    return ""
