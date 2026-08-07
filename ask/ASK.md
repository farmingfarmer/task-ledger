# Ask the ledger

A question box over confirmed state. The rule that makes it safe to ship in
a system whose whole claim is provenance:

> **The answer text is assembled from retrieved rows, never written by a
> model.**

Question understanding is a routing problem — which intent, which subject —
and routing is all a model is allowed to do here. Even that is currently a
regex, because a misrouted question gives a wrong-*shaped* answer while a
model writing prose can give a wrong-*content* one. Every sentence is built
from a database row and carries the task it came from.

```zsh
python3 ask/run_ask.py --as aad-dev --dsn "$DB_DSN" "what is blocked"
python3 ask/run_ask.py --as aad-dev --dsn "$DB_DSN" "status of the intake work"
python3 ask/run_ask.py --as aad-dev --dsn "$DB_DSN" --embed-model nomic-embed-text \
    "where did the extract get to"          # semantic subject matching
```

Also `GET /api/ask?q=...`, and the box at the top of the dashboard. The CLI
exits 1 when the ledger declined to answer, so a script can tell the
difference between "no" and "nothing".

## Three properties

**It refuses.** If the ledger holds only an unconfirmed claim about the
subject — a pooled signal that never passed a gate — the answer says so and
does not assert it. *"Someone said X, it is not confirmed"* is a different
sentence from *"X"*, and conflating them is how a provenance tool becomes a
rumour mill. The rendering puts the label before the quote, never after.
With nothing on file at all, it says that instead of guessing.

**It is scoped.** Retrieval runs through `visible_tasks(viewer)`, so a row
the asker may not see never enters the candidate set and therefore cannot
reach an answer. Filtering sentences after generation is how these systems
leak; this filters the *retrieval*. Held claims and the review queue are
self-only, as everywhere else.

**It cites.** Every fact carries its task and last board event; at
`full_trace` it also carries the evidence pointers behind that event, so
"why do you say that" terminates in a resource id and a hash. At progress
level the board citation still appears — the message behind it does not.

## Intents

`review_queue` · `blocked_what` / `blocked_on` · `overdue` · `due` ·
`waiting` · `who` · `meeting` · `history` · `status` · `search`

Order matters: *"waiting for me"* is a question about the review queue,
*"waiting on the IAM role"* is a question about a blocker, so the narrower
pattern is tried first. Both of those routings were wrong until real
questions were asked of it.

## Where a model could go later

Two places, neither of which touches the facts: a better **router** (an LLM
classifying intent and subject, with the regexes as the fallback), and
**subject matching** (already supported — pass `--embed-model` and ranking
becomes semantic instead of lexical). What a model must never do is write
the answer, because then the citation stops being a guarantee and becomes a
decoration.

## Validation

`test_ask.py` — 22 checks. The core takes an injected retriever, so routing,
citation, refusal, and answer shapes are all tested without a database. The
live section asserts the scoping that cannot be faked: a stranger asking
about someone else's work gets nothing, and the task's title never appears
anywhere in that answer.
