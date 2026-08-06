"""Reconciler: unconsumed signals + board -> ReviewPacket.

Implements matching_policy.yaml end to end:

  MATCH   blocking -> embed vs alias sets (max-sim) -> short-title penalty
          -> bands: auto_link / judge (top-k, via judge_prompts) / new-task.
          Guards: completion/cancellation are always judged; cross-owner
          matches are always judged; malformed judge output fails to
          'distinct'.
  GATE    per-signal-type op gates (signal_rules): trust proportional to
          blast radius. completion/cancellation demand the strictest match
          bar, owner self-reference or corroboration by independent
          evidence; assignment follows the explicit > self > inferred
          ladder; vague deadlines annotate instead of proposing; regressions
          and competing owners emit conflict flags, never silent applies.
  HOLD    signals that fail a corroboration gate are NOT consumed -- they
          stay pooled so a later window can corroborate them (the
          orchestrator's signal-return semantics, applied at birth).
  DEDUP   creates carry no target task, so aggregation's payload key cannot
          join two descriptions of the same new work. Packet-local create
          dedup runs first: near-verbatim titles merge outright, the gray
          band asks the merge judge, and with no judge both survive.
  AGGREGATE  same-op proposals on the same target combine noisy-OR (capped);
          per-op confidence floors from the policy drop the residue.
  EMIT    ReviewPacket with deterministic proposal ids, per-proposal
          rationales <=200 chars, flattened evidence, full signal lineage --
          validated against reconciliation_diff_schema.json before return.

Inputs are plain dicts (see reconcile() docstring) so the core is
storage-agnostic; board_io_pg.py adapts Postgres on both ends. The task<->task
merge scan from the policy is a separate periodic concern and is not part of
the per-user packet path. Python 3.8 compatible. Requires pyyaml + jsonschema.
"""

import datetime
import hashlib
import json
import os
import sys

import jsonschema
import yaml

import embedders


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _find(relpaths):
    for rel in relpaths:
        cand = os.path.normpath(os.path.join(_here(), rel))
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError("none of {0} found".format(relpaths))


def _import_judge_prompts():
    try:
        import judge_prompts
        return judge_prompts
    except ImportError:
        pol = _find([os.path.join("..", "policy"),
                     os.path.join("..", "..", "policy")])
        sys.path.insert(0, pol)
        import judge_prompts
        return judge_prompts


judge_prompts = _import_judge_prompts()

DEFAULT_POLICY_PATH_CANDIDATES = [
    os.path.join("..", "policy", "matching_policy.yaml"),
    "matching_policy.yaml",
]
DEFAULT_SCHEMA_PATH_CANDIDATES = [
    os.path.join("..", "contracts", "reconciliation_diff_schema.json"),
    os.path.join("..", "reconciliation_diff_schema.json"),
]

CREATE_OPS = ("create_task", "add_subtask")

FORWARD = {
    ("proposed", "in_progress"), ("proposed", "blocked"),
    ("proposed", "cancelled"), ("in_progress", "blocked"),
    ("blocked", "in_progress"), ("in_progress", "completed"),
    ("blocked", "completed"), ("in_progress", "cancelled"),
    ("blocked", "cancelled"), ("proposed", "completed"),
}


def _sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _clip(text, n=200):
    text = text or ""
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def _append_note(rationale, note, n=200):
    """Append a note to a rationale within the schema's 200-char cap,
    clipping the SENTENCE rather than the note -- the note is the part the
    reviewer cannot reconstruct from the card."""
    base = _clip(rationale or "", max(n - len(note) - 1, 0))
    return (base + " " + note).strip()[:n]


class Reconciler(object):
    """embed(texts)->vectors, judge_llm(messages)->text are injected.
    judge_llm=None runs match-only mode: judge-band signals are held."""

    def __init__(self, embed, judge_llm=None, policy_path=None,
                 schema_path=None, clock=None):
        with open(policy_path or _find(DEFAULT_POLICY_PATH_CANDIDATES)) as f:
            self.policy = yaml.safe_load(f)
        with open(schema_path or _find(DEFAULT_SCHEMA_PATH_CANDIDATES)) as f:
            self.schema = json.load(f)
        self.embed = embed
        self.judge_llm = judge_llm
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc))

    # ------------------------------------------------------------ reconcile

    def reconcile(self, user_ref, signals, tasks, board_version,
                  cadence_run_id, all_tasks=None):
        """signals: rows shaped like v_unconsumed_signals + raw signal dict:
             {signal_id, signal_type, title, description, status_signal,
              confidence, deadline_{date,precision,raw}, blocker_desc,
              blocker_party, owner_candidates: [...], evidence: [...]}
           tasks: this user's matchable board:
             {task_id, title, status, owner_ref, deadline, aliases: [...],
              age_days, open_blockers: [{blocker_id, description}]}
           all_tasks: every board's tasks -- blocking-edge resolution and
             cross-board duplicate annotation (defaults to tasks, which
             makes both checks a no-op rather than wrong).
           Returns (packet, report). report: consumed/held signal ids,
           annotations, merges, drop log."""
        all_tasks = all_tasks if all_tasks is not None else tasks
        report = {"consumed": [], "held": [], "dropped": [],
                  "annotations": [], "merged": [], "judge_calls": 0}
        draft = []   # gate outputs before aggregation

        task_vecs = self._embed_alias_sets(tasks)

        for sig in signals:
            match = self._match(sig, tasks, task_vecs, report)
            outcome = self._gate(sig, match, tasks, all_tasks, signals,
                                 report)
            if outcome == "held":
                report["held"].append(sig["signal_id"])
                continue
            report["consumed"].append(sig["signal_id"])
            if isinstance(outcome, dict):
                draft.append(outcome)

        draft = self._dedupe_creates(draft, report)
        proposals = self._aggregate(draft, report, cadence_run_id)
        self._annotate_cross_board(proposals, tasks, all_tasks, report)
        packet = {
            "packet_id": _sha(user_ref, cadence_run_id, board_version,
                              *sorted(report["consumed"])),
            "user_ref": user_ref,
            "generated_at": self.clock().isoformat(),
            "cadence_run_id": cadence_run_id,
            "board_version": int(board_version),
            "expires_at": None,
            "proposals": proposals,
        }
        jsonschema.validate(packet, self.schema)
        return packet, report

    # ------------------------------------------------------------- matching

    def _embed_alias_sets(self, tasks):
        texts, index = [], []
        cap = self.policy["embedding"]["task_alias_max"]
        for t in tasks:
            aliases = [t["title"]] + list(t.get("aliases") or [])[:cap]
            for a in aliases:
                texts.append(a)
                index.append(t["task_id"])
        vecs = self.embed(texts) if texts else []
        out = {}
        for tid, vec in zip(index, vecs):
            out.setdefault(tid, []).append(vec)
        return out

    def _match(self, sig, tasks, task_vecs, report):
        """-> {"kind": "linked"|"subtask"|"new", "task_id", "similarity",
               "note", "judge_conf"}"""
        th = self.policy["thresholds"]
        if not tasks:
            return {"kind": "new", "task_id": None, "similarity": 0.0,
                    "note": "no matchable tasks on board", "judge_conf": None}

        text = sig["title"] + "\n" + (sig.get("description") or "")
        svec = self.embed([text])[0]
        scored = []
        for t in tasks:
            sims = [embedders.cosine(svec, v)
                    for v in task_vecs.get(t["task_id"], [])]
            sim = max(sims) if sims else 0.0
            if len(sig["title"].split()) <= th["short_title_penalty"]["max_tokens"]:
                sim -= th["short_title_penalty"]["penalty"]
            scored.append((sim, t))
        scored.sort(key=lambda p: -p[0])
        best_sim, best = scored[0]

        never_auto = (sig["signal_type"]
                      in th["auto_link_guards"]["never_auto_link_types"])
        owners = {c["person_ref"] for c in sig.get("owner_candidates") or []}
        cross_owner = bool(owners) and best["owner_ref"] not in owners

        if (best_sim >= th["auto_link"] and not never_auto
                and not (cross_owner
                         and th["auto_link_guards"]["never_auto_link_cross_owner"])):
            return {"kind": "linked", "task_id": best["task_id"],
                    "similarity": best_sim, "note": None, "judge_conf": None}

        if best_sim >= th["judge_floor"] or (never_auto
                                             and best_sim >= th["judge_floor"] - 0.1):
            verdict = self._judge(sig, [t for _, t in
                                        scored[:self.policy
                                               ["candidate_generation"]
                                               ["top_k_to_judge"]]],
                                  {t["task_id"]: s for s, t in scored},
                                  report)
            if verdict is not None:
                v, conf, tid, factor = verdict
                if conf >= th["judge_accept_min"]:
                    if v == "same_task":
                        return {"kind": "linked", "task_id": tid,
                                "similarity": best_sim, "note": factor,
                                "judge_conf": conf}
                    if v == "subtask_of":
                        return {"kind": "subtask", "task_id": tid,
                                "similarity": best_sim, "note": factor,
                                "judge_conf": conf}

        note = "closest match {0} ({1:.2f}) below merge threshold".format(
            best["task_id"], max(best_sim, 0.0))
        return {"kind": "new", "task_id": None, "similarity": best_sim,
                "note": note, "judge_conf": None}

    def _judge(self, sig, candidates, sims, report):
        if self.judge_llm is None:
            return None
        cand_dicts = [{
            "task_id": t["task_id"], "title": t["title"],
            "status": t.get("status"), "owner": t.get("owner_ref"),
            "similarity": sims.get(t["task_id"], 0.0),
            "description": "", "aliases": t.get("aliases") or [],
            "last_activity": "",
        } for t in candidates]
        msgs = judge_prompts.build_signal_judge_messages(
            {"signal_type": sig["signal_type"], "title": sig["title"],
             "description": sig.get("description"),
             "attribution": _attribution_line(sig),
             "evidence_excerpts": []},
            cand_dicts)
        report["judge_calls"] += 1
        raw = self.judge_llm(msgs)
        try:
            r = judge_prompts.parse_judge_response(raw)
        except (ValueError, KeyError):
            return ("distinct", 1.0, None, "malformed judge output")
        return (r["verdict"], r["confidence"], r.get("matched_task_id"),
                r["deciding_factor"])

    # ---------------------------------------------------------------- gates

    def _gate(self, sig, match, tasks, all_tasks, all_signals, report):
        stype = sig["signal_type"]
        rules = self.policy["signal_rules"].get(stype) or {}
        if sig["confidence"] < rules.get("min_conf", 0.6):
            report["dropped"].append({"signal_id": sig["signal_id"],
                                      "reason": "below_min_conf"})
            return None
        task = _task_by_id(tasks, match["task_id"])

        if stype == "task_mention":
            if task is not None:
                report["annotations"].append(
                    {"kind": "alias", "task_id": task["task_id"],
                     "alias": sig["title"],
                     "source_signal_id": sig["signal_id"]})
            return None

        if stype == "proposal":
            if match["kind"] == "linked":
                report["annotations"].append(
                    {"kind": "alias", "task_id": match["task_id"],
                     "alias": sig["title"],
                     "source_signal_id": sig["signal_id"]})
                return None
            return self._mk_create(sig, match)

        if stype == "assignment":
            return self._gate_assignment(sig, match, task)

        if stype == "status_update":
            return self._gate_status(sig, match, task)

        if stype == "deadline_signal":
            return self._gate_deadline(sig, match, task, report)

        if stype == "blocker":
            if task is None or task["status"] in ("completed", "cancelled"):
                report["dropped"].append({"signal_id": sig["signal_id"],
                                          "reason": "no_open_target"})
                return None
            return self._mk(sig, "add_blocker", task["task_id"], {
                "kind": "add_blocker",
                "description": sig.get("blocker_desc") or sig["title"],
                "blocking_party_ref": sig.get("blocker_party"),
                "blocking_task_id": self._resolve_blocking_task(
                    sig, all_tasks),
            }, "You said \u201c{0}\u201d is stuck: {1}".format(
                task["title"], sig.get("blocker_desc") or sig["title"]))

        if stype == "blocker_resolved":
            return self._gate_blocker_resolved(sig, task, report)

        if stype in ("completion_claim", "cancellation"):
            return self._gate_closure(sig, match, task, all_signals, report)

        report["dropped"].append({"signal_id": sig["signal_id"],
                                  "reason": "unhandled_type"})
        return None

    def _gate_assignment(self, sig, match, task):
        cands = sig.get("owner_candidates") or []
        best = max(cands, key=lambda c: c["confidence"]) if cands else None
        if best is None:
            return None
        if match["kind"] == "new":
            return self._mk_create(sig, match, owner_ref=best["person_ref"])
        if task is None:
            return None
        if best["basis"] == "explicit_assignment":
            if task.get("owner_ref") == best["person_ref"]:
                return None   # already theirs; nothing to propose
            return self._mk(sig, "reassign", task["task_id"], {
                "kind": "reassign",
                "from_owner_ref": task.get("owner_ref"),
                "to_owner_ref": best["person_ref"],
                "basis": "explicit_assignment",
            }, "\u201c{0}\u201d was assigned to a new owner in discussion."
               .format(task["title"]))
        if best["basis"] == "self_reference":
            if task.get("owner_ref") is None:
                return self._mk(sig, "reassign", task["task_id"], {
                    "kind": "reassign", "from_owner_ref": None,
                    "to_owner_ref": best["person_ref"],
                    "basis": "self_reference",
                }, "\u201c{0}\u201d was claimed by its speaker; it currently "
                   "has no owner.".format(task["title"]))
            if task["owner_ref"] != best["person_ref"]:
                p = self._mk(sig, "reassign", task["task_id"], {
                    "kind": "reassign",
                    "from_owner_ref": task["owner_ref"],
                    "to_owner_ref": best["person_ref"],
                    "basis": "self_reference",
                }, "Someone claimed \u201c{0}\u201d, which already has an "
                   "owner.".format(task["title"]))
                p["conflict"] = {"type": "competing_owner",
                                 "detail": "Task is owned by {0}; a "
                                 "self-claim came from {1}.".format(
                                     task["owner_ref"], best["person_ref"])}
                return p
        return None   # inferred: annotation territory, never a proposal

    def _gate_status(self, sig, match, task):
        to = sig.get("status_signal")
        if task is None or to is None or task["status"] == to:
            return None
        p = self._mk(sig, "update_status", task["task_id"], {
            "kind": "update_status",
            "from_status": task["status"],
            "to_status": to,
        }, "Recent discussion indicates \u201c{0}\u201d is now {1}.".format(
            task["title"], to.replace("_", " ")))
        if (task["status"], to) not in FORWARD:
            p["conflict"] = {"type": "stale_evidence",
                             "detail": "Board says {0}; the discussion "
                             "implies {1}, a regression.".format(
                                 task["status"], to)}
        return p

    def _gate_deadline(self, sig, match, task, report):
        prec = sig.get("deadline_precision")
        date = sig.get("deadline_date")
        raw = sig.get("deadline_raw")
        if task is None or prec is None:
            return None
        if prec == "vague" or date is None:
            report["annotations"].append(
                {"kind": "deadline_note", "task_id": task["task_id"],
                 "raw_text": raw, "source_signal_id": sig["signal_id"]})
            return None
        if str(task.get("deadline") or "") == str(date):
            return None
        p = self._mk(sig, "update_deadline", task["task_id"], {
            "kind": "update_deadline",
            "from_deadline": task.get("deadline"),
            "to_deadline": str(date),
            "raw_text": raw,
        }, "\u201c{0}\u201d was given the timeframe \u201c{1}\u201d.".format(
            task["title"], raw))
        cands = sig.get("owner_candidates") or []
        speaker_is_owner = any(c["person_ref"] == task.get("owner_ref")
                               for c in cands)
        explicit = any(c["basis"] == "explicit_assignment" for c in cands)
        if cands and not speaker_is_owner and not explicit:
            p["conflict"] = {"type": "competing_owner",
                             "detail": "Deadline suggested by someone other "
                             "than the task owner."}
        return p

    def _gate_blocker_resolved(self, sig, task, report):
        if task is None:
            return None
        open_blockers = task.get("open_blockers") or []
        text = (sig.get("blocker_desc") or sig["title"]).lower()
        best_id, best_overlap = None, 0.0
        for b in open_blockers:
            overlap = _token_overlap(text, (b.get("description") or "").lower())
            if overlap > best_overlap:
                best_overlap, best_id = overlap, b["blocker_id"]
        if best_id is None or best_overlap < 0.5:
            return "held"
        return self._mk(sig, "resolve_blocker", task["task_id"], {
            "kind": "resolve_blocker", "blocker_id": str(best_id),
        }, "The blocker on \u201c{0}\u201d was said to be cleared.".format(
            task["title"]))

    def _gate_closure(self, sig, match, task, all_signals, report):
        rules = self.policy["signal_rules"][sig["signal_type"]]
        th = self.policy["thresholds"]
        op = ("complete_task" if sig["signal_type"] == "completion_claim"
              else "cancel_task")
        if task is None:
            # A closure that failed to match is far more likely a matching
            # failure than a real new-work signal; hold it for a retry
            # rather than consuming it into oblivion.
            return "held"
        if task["status"] in ("completed", "cancelled"):
            return None   # already there; silence, not a duplicate card
        strict_ok = (match["kind"] == "linked"
                     and (match["judge_conf"] is None
                          or match["judge_conf"] >= 0.85))
        if not strict_ok:
            return "held"
        owner_claims = any(
            c["basis"] in ("self_reference", "explicit_assignment")
            and c["person_ref"] == task.get("owner_ref")
            for c in sig.get("owner_candidates") or [])
        young = (task.get("age_days") is not None
                 and task["age_days"] < 1)
        if owner_claims and not young:
            pass   # single owner-claim suffices
        else:
            n_independent = _independent_support(sig, all_signals)
            if n_independent < self.policy["corroboration"]["independent_source_min"]:
                return "held"
        rationale = ("\u201c{0}\u201d was reported {1}.".format(
            task["title"],
            "finished" if op == "complete_task" else "called off"))
        return self._mk(sig, op, task["task_id"], {
            "kind": op, "completion_note": None,
        }, rationale)

    # ------------------------------------------------------------ assembly

    def _resolve_blocking_task(self, sig, all_tasks):
        desc = (sig.get("blocker_desc") or "").lower()
        if not desc:
            return None
        best, score = None, 0.0
        for t in all_tasks:
            ov = _token_overlap(desc, t["title"].lower())
            if ov > score:
                score, best = ov, t
        return best["task_id"] if best is not None and score >= 0.5 else None

    def _mk_create(self, sig, match, owner_ref=None):
        status = sig.get("status_signal") or "proposed"
        if status in ("completed", "cancelled"):
            status = "proposed"
        payload = {
            "kind": "create_task",
            "title": sig["title"],
            "description": sig.get("description"),
            "status": status,
            "owner_ref": owner_ref or _best_owner(sig),
            "deadline": (str(sig["deadline_date"])
                         if sig.get("deadline_date") else None),
            "parent_task_id": None,
            "match_note": match.get("note"),
        }
        op = "create_task"
        if match["kind"] == "subtask":
            payload = {"kind": "add_subtask",
                       "parent_task_id": match["task_id"],
                       "title": sig["title"],
                       "description": sig.get("description")}
            op = "add_subtask"
        return self._mk(sig, op, match.get("task_id") if op == "add_subtask"
                        else None, payload,
                        "New work surfaced in discussion: \u201c{0}\u201d."
                        .format(sig["title"]))

    def _mk(self, sig, op, target_task_id, payload, rationale):
        return {
            "op": op,
            "target_task_id": target_task_id,
            "rationale": _clip(rationale),
            "confidence": float(sig["confidence"]),
            "source_signals": [sig["signal_id"]],
            "evidence": list(sig.get("evidence") or []),
            "conflict": None,
            "payload": payload,
        }

    def _annotate_cross_board(self, proposals, tasks, all_tasks, report):
        """Annotate unowned creates that look like work already on someone
        else's board.

        An unassigned "someone should do this" reaches a packet as a
        create with no owner, and the reviewer has no way to know a
        colleague is already carrying it. So unowned creates are matched
        against every board, not just this one, and the finding is appended
        to the rationale. Annotation ONLY: the proposal still stands, is
        never auto-rejected, and is never suppressed -- similar titles are
        evidence, and only the human knows whether it is the same work.

        Note for a future multi-tenant deployment: this states that another
        board holds a similar task, which the access layer would otherwise
        keep private (ACCESS_CONTROL.md's no-count-leakage rule). Scoping
        it is one query -- feed all_tasks from visible_tasks(viewer)
        instead of load_all_boards()."""
        cfg = self.policy.get("cross_board_duplicate_check") or {}
        floor = cfg.get("title_similarity", 0.80)
        targets = [p for p in proposals if p["op"] == "create_task"
                   and p["payload"].get("owner_ref") is None]
        mine = {t["task_id"] for t in tasks}
        elsewhere = [t for t in all_tasks if t["task_id"] not in mine
                     and t.get("title")]
        if not targets or not elsewhere:
            return
        vecs = self.embed([_create_title(p) for p in targets]
                          + [t["title"] for t in elsewhere])
        board_vecs = vecs[len(targets):]
        for prop, pvec in zip(targets, vecs[:len(targets)]):
            best, best_sim = None, floor
            for cand, cvec in zip(elsewhere, board_vecs):
                sim = embedders.cosine(pvec, cvec)
                if sim >= best_sim:
                    best, best_sim = cand, sim
            if best is None:
                continue
            ref = (best.get("user_ref") or best.get("owner_ref")
                   or "another user")
            prop["rationale"] = _append_note(
                prop["rationale"],
                "(A similar task exists on {0}'s board.)".format(ref))
            report["annotations"].append(
                {"kind": "cross_board_duplicate", "task_id": best["task_id"],
                 "board_ref": ref, "similarity": round(best_sim, 3),
                 "proposal_title": _create_title(prop)})

    def _dedupe_creates(self, draft, report):
        """Merge same-op create proposals describing the same new work.

        Creates have no target_task_id, so _aggregate's (op, target,
        payload) key treats every wording as a separate card: the first
        live run proposed one piece of new work twice, once as a vague
        restatement and once as a specific description. Similarity alone
        cannot decide this (that pair scored 0.805 on nomic-embed-text,
        while staging-vs-production creates, which must NOT merge, score
        0.887), so the policy's bands apply -- near-verbatim
        merges outright, the gray band goes to the merge judge, and with no
        judge both survive. The strongest proposal is the survivor and
        absorbs the rest; nothing is dropped silently."""
        cfg = (self.policy["aggregation"].get("packet_local_create_dedup")
               or {})
        cap = self.policy["aggregation"]["noisy_or_cap"]
        creates = [p for p in draft if p["op"] in CREATE_OPS]
        if len(creates) < 2:
            return draft
        order = sorted(creates, key=lambda p: -p["confidence"])
        vecs = self.embed([_create_title(p) for p in order])
        survivors, absorbed, counts = [], set(), {}
        for prop, vec in zip(order, vecs):
            twin = None
            for kept, kvec in survivors:
                if (kept["op"] != prop["op"]
                        or kept["target_task_id"] != prop["target_task_id"]):
                    continue
                sim = embedders.cosine(kvec, vec)
                if sim >= cfg.get("auto_merge", 0.93) or self._judge_merge(
                        kept, prop, sim, cfg, report):
                    twin, twin_sim = kept, sim
                    break
            if twin is None:
                survivors.append((prop, vec))
                continue
            _absorb_create(twin, prop, cap)
            absorbed.add(id(prop))
            counts[id(twin)] = counts.get(id(twin), 1) + 1
            report["merged"].append(
                {"kept": _create_title(twin),
                 "absorbed": _create_title(prop),
                 "similarity": round(twin_sim, 3),
                 "signals": list(prop["source_signals"])})
        for prop, _ in survivors:
            n = counts.get(id(prop))
            if n:
                prop["rationale"] = _append_note(
                    prop["rationale"],
                    "({0} mentions of this work merged.)".format(n))
        return [p for p in draft if id(p) not in absorbed]

    def _judge_merge(self, kept, other, sim, cfg, report):
        """Gray-band arbiter for two pending creates, using the same merge
        judge as the periodic task<->task scan. No judge -> False: two
        cards is noise, a wrong merge hides work the reviewer never sees."""
        if sim < cfg.get("judge_floor", 0.78) or self.judge_llm is None:
            return False
        msgs = judge_prompts.build_merge_judge_messages(
            _as_merge_block("pending-kept", kept),
            _as_merge_block("pending-other", other))
        report["judge_calls"] += 1
        try:
            r = judge_prompts.parse_judge_response(self.judge_llm(msgs))
        except (ValueError, KeyError):
            return False   # malformed judge output never merges
        return (r["verdict"] == "same_task"
                and r["confidence"] >= cfg.get("judge_accept_min", 0.85))

    def _aggregate(self, draft, report, run_salt=""):
        floors = self.policy["per_op_confidence_floor"]
        cap = self.policy["aggregation"]["noisy_or_cap"]
        grouped = {}
        for p in draft:
            key = (p["op"], p["target_task_id"],
                   json.dumps(p["payload"], sort_keys=True)
                   if p["op"] in ("create_task", "add_subtask")
                   else json.dumps({k: p["payload"].get(k)
                                    for k in ("to_status", "to_deadline",
                                              "to_owner_ref", "blocker_id",
                                              "description")},
                                   sort_keys=True))
            if key not in grouped:
                grouped[key] = p
            else:
                g = grouped[key]
                combined = 1.0 - (1.0 - g["confidence"]) * (1.0 - p["confidence"])
                g["confidence"] = min(cap, combined)
                g["source_signals"] = sorted(
                    set(g["source_signals"]) | set(p["source_signals"]))
                seen = {e["resource_id"] for e in g["evidence"]}
                g["evidence"].extend(e for e in p["evidence"]
                                     if e["resource_id"] not in seen)
                if p["conflict"] and not g["conflict"]:
                    g["conflict"] = p["conflict"]
        out = []
        for p in grouped.values():
            if p["confidence"] < floors.get(p["op"], 0.6):
                report["dropped"].append(
                    {"signal_id": ",".join(p["source_signals"]),
                     "reason": "below_op_floor:{0}".format(p["op"])})
                continue
            p["confidence"] = round(min(p["confidence"], 1.0), 3)
            p["proposal_id"] = _sha(run_salt, p["op"], p["target_task_id"],
                                    *p["source_signals"])
            out.append(p)
        out.sort(key=lambda p: (0 if p["conflict"] else 1, -p["confidence"]))
        return out


# ---------------------------------------------------------------- utilities

def _create_title(prop):
    return prop["payload"].get("title") or ""


def _absorb_create(keep, drop, cap):
    """Fold one create proposal into another: pooled confidence, unioned
    lineage and evidence, and any field the survivor left empty. That last
    part matters -- an ownerless create is invisible on every board, so a
    twin that names an owner contributes it."""
    combined = 1.0 - (1.0 - keep["confidence"]) * (1.0 - drop["confidence"])
    keep["confidence"] = min(cap, combined)
    keep["source_signals"] = sorted(
        set(keep["source_signals"]) | set(drop["source_signals"]))
    seen = {e["resource_id"] for e in keep["evidence"]}
    keep["evidence"].extend(e for e in drop["evidence"]
                            if e["resource_id"] not in seen)
    for field in ("owner_ref", "deadline", "description"):
        # `field in keep` guards the schema: add_subtask payloads have no
        # owner_ref or deadline, and additionalProperties is false.
        if (field in keep["payload"] and keep["payload"][field] is None
                and drop["payload"].get(field) is not None):
            keep["payload"][field] = drop["payload"][field]
    if drop["conflict"] and not keep["conflict"]:
        keep["conflict"] = drop["conflict"]


def _as_merge_block(pseudo_id, prop):
    """A pending create rendered as the merge judge's task block. The id is
    synthetic: these are proposals, not board rows."""
    p = prop["payload"]
    return {"task_id": pseudo_id, "title": p.get("title"),
            "status": p.get("status") or "proposed",
            "owner": p.get("owner_ref") or "unowned",
            "description": p.get("description"),
            "deadline": p.get("deadline") or "(none)",
            "history_summary": "proposed this run; no confirmed history"}


def _task_by_id(tasks, tid):
    for t in tasks:
        if t["task_id"] == tid:
            return t
    return None


def _best_owner(sig):
    cands = sig.get("owner_candidates") or []
    if not cands:
        return None
    return max(cands, key=lambda c: c["confidence"])["person_ref"]


def _attribution_line(sig):
    cands = sig.get("owner_candidates") or []
    if not cands:
        return "unattributed"
    best = max(cands, key=lambda c: c["confidence"])
    return "{0} ({1})".format(best["person_ref"], best["basis"])


def _token_overlap(a, b):
    ta = set(a.split()) - embedders._STOP
    tb = set(b.split()) - embedders._STOP
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _independent_support(sig, all_signals):
    """Independent = distinct primary-evidence resource ids across signals
    that plausibly describe the same closure (same type, overlapping title)."""
    primary = {(sig.get("evidence") or [{}])[0].get("resource_id")}
    for other in all_signals:
        if other["signal_id"] == sig["signal_id"]:
            continue
        if other["signal_type"] != sig["signal_type"]:
            continue
        if _token_overlap(other["title"].lower(),
                          sig["title"].lower()) < 0.5:
            continue
        rid = (other.get("evidence") or [{}])[0].get("resource_id")
        if rid:
            primary.add(rid)
    return len(primary)
