# Digest: the ledger, addressed to a person

The review card asks for a decision. The dashboard waits to be opened. A
digest asks nothing and arrives anyway — which is the point, because a tool
you have to remember to open is a tool that gets forgotten. Nothing here is
new information; it is the same ledger, chosen and phrased so that reading
it takes thirty seconds.

```zsh
python3 digest/run_digest.py --as aad-dev --dsn "$DB_DSN"                # text
python3 digest/run_digest.py --as aad-dev --dsn "$DB_DSN" --format html --out ~/d.html
python3 digest/run_digest.py --project proj-drift --dsn "$DB_DSN" --to-recipients
python3 digest/run_digest.py --as aad-dev --dsn "$DB_DSN" --format card  # Teams payload
```

## Two kinds, two rules

**Personal** — one person, their own week: what is waiting for their review
and how stale it has gone, what they confirmed, what is past its date, what
they are blocked on and who is blocked on them, claims heard but held for
corroboration, work gone quiet, what this week's conversations produced, and
the talk-to-tracking median. Self-only, the same bar as the `pending`,
`held` and `calibration` endpoints: it carries unreviewed claims and a
person's own decisions, and no grant opens those.

**Project** — for whoever the project is currently shared with: what
changed, what is overdue or blocked, where it stands. Redacted at progress
level **by construction**: `load_project()` never SELECTs a rationale, a
decider, a confidence or an evidence pointer, so a stakeholder digest cannot
quote a meeting they were not in even by mistake. Redaction that lives in
the query cannot be forgotten by a later renderer; redaction that lives in a
filter can.

Recipients come from `v_current_grants`, so a grant that expired or was
revoked simply stops arriving — there is no second list to prune.

## What makes it readable

- **A quiet week sends nothing.** `quiet: true` and the runner exits without
  output unless `--even-if-quiet`. A digest that arrives every week whether
  or not it has anything to say is a digest people filter into a folder.
- **The headline leads with what is urgent**, not with what is cheerful:
  overdue, blocked, and a stale review queue outrank accomplishments. It
  counts *things* rather than lines, so three proposals described in one
  sentence is "3 items".
- **Sections are capped** at `MAX_LINES_PER_SECTION` and state what was left
  out, so a bad week cannot produce a wall of text.
- **Every title is escaped** in the HTML rendering: a task title comes from
  a model reading someone's messages, and is never trusted as markup.

## Delivery

Deliberately unopinionated. The builder renders to text, HTML, or an
Adaptive Card payload; the runner writes to stdout or a file. Nothing in
this module sends mail, so nothing in it can send a digest to the wrong
address. The card is sized against the same 28 KB Teams ceiling
`review_card.py` obeys, and fails at build rather than on delivery, so the
bot host can deliver it the same way it delivers a review card.

The cadence tick writes the week's digest as HTML on `TL_DIGEST_DAY`
(0=Monday, default Friday) into `TL_DIGEST_DIR` (default
`~/task-ledger-digests`). A failure there is logged and never fails the
tick: a digest only summarises work that is already safely recorded.

## Validation

`test_digest.py` — 31 checks. The builder and renderers are pure, so most
run without a database: section assembly and ordering, the urgent-first
headline and its counting, quiet weeks, truncation, HTML escaping, card
size (including that an oversized card raises). The live section asserts
the part that cannot be faked — that the project loader's output contains
no `rationale`, `confidence`, `decided_by`, `signal_id` or `content_hash`,
and that recipients track current grants.
