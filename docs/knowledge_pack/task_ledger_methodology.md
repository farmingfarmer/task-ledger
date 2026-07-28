---
id: task_ledger_methodology
display_name: Task Ledger Methodology
aliases: [task ledger principles, task ledger lessons]
type: documentation
status: active
owner: Alex
related: [task_ledger]
---

# Task Ledger Methodology

These are the transferable engineering principles from Task Ledger, each
earned from a real incident in the project's history.

The AI proposes, code disposes. When mistral-small read "the deadline is
dropped" as a new deadline signal three out of three times at
temperature zero despite targeted prompt guidance, the fix moved from
prompting to a deterministic worker guard with a recorded drop reason.
Blind spots that survive prompting graduate to code.

Trust proportional to blast radius. Wrongly closing work is the costliest
error, so completion claims face the strictest gate: owner self-claims
pass on one source, hearsay needs two independent sources, and a task
created seconds ago holds even its owner's claim. In live testing the
youth gate held a task's own creation echo, exactly as designed.

Fail closed into the pool. Uncertain signals are held, not guessed at:
a confused judge causes delay, never damage. Held signals re-enter
automatically because held is just unconsumed.

Assert the invariant a module owns, not transient state. Both Windows
port failures were test-isolation bugs: an extraction test asserted
signals were currently unconsumed when the reconciler had legitimately
consumed them, and a reconciler test used a constant run id so reruns on
a pristine database became byte-identical replays that correctly wrote
zero rows. The system was innocent both times; the assertions were wrong.

Append-only ledgers debug themselves. The Windows failure was diagnosed
in one read of the pooled-returned-taken event trail; the audit design
paid for itself as an engineering tool before any auditor saw it.

Evaluate on fixed seeds, hold one out. Prompt v1.0 to v1.1 moved recall
76 to 97 percent on the development seed and held 94 on an unseen seed;
every failure the scorer named (mention echoes missed, announcement
dates extracted as deadlines, hedged completions suppressed) became a
named fix. Verify the harness before the model: a cheating LLM must
score 100 percent and an empty one 0.

Silence is a valid output, and degraded is not failure. The cadence tick
exits zero on empty days and down AIs; notifications fire only when
proposals await.

Salt derived identities with their run. Unsalted proposal ids collided
across packets and silently wrote zero proposals; content-derived ids
need run context when the same content can legitimately recur.
