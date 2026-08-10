# Two ledgers: real and synthetic

The live ledger holds real colleague communication. Anything that needs to
be *exercised* — a pipeline change, a new chart, a demo, an eval run —
should run against synthetic material instead, with the same cast and the
same shapes but work that does not exist.

Switching is a matter of two environment variables, because nothing in the
system knows which profile it is running under.

```zsh
# ---- real -------------------------------------------------------------
export DB_DSN="host=localhost port=5432 dbname=taskintel user=$(whoami)"
export TL_INBOX="$HOME/task-ledger-inbox"

# ---- synthetic --------------------------------------------------------
export DB_DSN="host=localhost port=5432 dbname=taskintel_synth user=$(whoami)"
export TL_INBOX="$HOME/task-ledger-synthetic-inbox"
```

Everything then follows the environment: `cadence_tick.py`, `run_reconcile.py`,
`cli/review.py --dsn "$DB_DSN"`, the dashboard, the digest, ask.

## The dashboard shows both, and says which

Everything else takes one ledger at a time. The dashboard takes both, so a
demo can move between real extraction and a synthetic set wide enough to
cover interactions a real week did not happen to contain — without editing
an environment and restarting mid-sentence.

```zsh
export DB_DSN="host=localhost port=5432 dbname=taskintel user=$(whoami)"
export TL_SYNTH_DSN="host=localhost port=5432 dbname=taskintel_synth user=$(whoami)"
python3 integration/dashboard/dashboard_api.py     # switch is in the header
```

With `TL_SYNTH_DSN` unset the switch does not appear and the dashboard
behaves exactly as it did before. Optional: `TL_DEFAULT_PROFILE=synthetic`
opens on the synthetic ledger, which is the safer default for a laptop
that gets opened in front of an audience.

**The client picks a name, never a connection string.** A request carries
`?profile=real` or `?profile=synthetic`; anything else falls back to the
default, because an endpoint that accepted a DSN from the page would point
the dashboard at any database the host can reach. The switch is also not a
way around authorization — every query still joins through
`visible_tasks(viewer)`, and the SELF-ONLY endpoints stay self-only under
either profile. Both properties are asserted in `test_dashboard.py`.

The synthetic profile paints a banner across the header whenever it is
active. Colour alone would fail a projector or a colourblind viewer, so it
says the word *synthetic* too: the one failure this feature must never
allow is an audience that cannot tell which ledger it is looking at.

## A third ledger: what Copilot itself claims

Copilot will export per-person task claims already structured -- person,
task, status, progress, blockers, next_action, claim_type, confidence,
sources. That is a better input than a recap and, in places, better than
raw transcript: specific, per-person, and separating progress from blockers
from next actions.

```zsh
python3 scripts/copilot_claims_ingest.py claims.json --dry-run
createdb taskintel_copilot   # + schema.sql, access_control.sql, migrations
export TL_COPILOT_DSN="host=localhost port=5432 dbname=taskintel_copilot user=$(whoami)"
python3 scripts/copilot_claims_ingest.py claims.json --dsn "$TL_COPILOT_DSN" --bootstrap
```

It does **not** run the extractor: Copilot already extracted, and a second
pass over a paraphrase only loses more. The ingest writes the same batch
shape the worker writes, against the same contract, so pooling,
reconciliation, gates and review are unchanged. Copilot becomes the
extractor; this stays the system of record.

`--bootstrap` matters on a first import. An export is mostly progress and
blockers on work already under way, and against an empty ledger those have
nothing to attach to -- `update_status` needs a target task -- so they are
consumed and propose nothing. Bootstrap maps every claim to a type that can
create, so the first import establishes that the work exists.

**What it refuses to pretend.** Each record asserts
`"evidence_strength": "transcript_supported"` and lists meeting names --
no message id, no utterance, no span, and the assistant will not release
the transcript. That is an assertion that evidence exists, not evidence.
So these land as evidence type `assistant_claim`, which the provenance rail
must never offer as a trace-to-source; the hash is over the exported record,
which makes a quietly edited export detectable and claims nothing more;
every owner basis is `inferred`, because `self_reference` means somebody was
heard and nothing here was; and confidence is capped at 0.75 -- above the
0.60 floor so these claims do reach a review card, below the 0.80 and 0.85
floors so an unverifiable claim is structurally incapable of proposing that
work is complete, cancelled or merged away.

The dashboard shows it as a third button, with its own caution banner, and
it is **not writable from the browser**: it holds real colleague content, so
it keeps the read-only session for the same reason the real ledger does.
Nothing is on its board until a person agrees -- which is the demo.

## The nudge popup

The dashboard asks the two questions the Teams card asks: what was heard
that you have not ruled on, and what you took on that has gone quiet.
Decisions are **recorded only on the synthetic profile** -- on the real
ledger the buttons are disabled and say why, because the read-only session
is what keeps a browser away from colleague data.

```zsh
export TL_QUIET_DAYS=0    # a freshly seeded demo has every event stamped
                          # today, so at the 7-day default nothing is quiet
                          # and that half of the popup renders empty
```

An accept goes through `cli/review.py`'s `apply_accept()` into
`apply_task_event()` -- the browser adds a caller, not a second write path.
A nudge writes `actor_type='manual_edit'` with a NULL `decision_id`, the
place the schema has kept for hand-typed truth since the beginning, so the
provenance rail can always tell it from a model proposal a human accepted.

## Making the synthetic ledger demo-ready

Extraction alone leaves signals and proposals, so the dashboard would show
a review queue over an empty board. `demo_seed_synthetic.py` walks those
proposals through the same decision path a person would — several people,
several rounds, so later rounds match against the board earlier ones built
— and leaves a queue behind on purpose:

```zsh
python3 scripts/demo_seed_synthetic.py --dsn "$TL_SYNTH_DSN"
```

Pass `--judge-model mistral-small3.1` unless you want a board with
nothing finished on it: without a judge the reconciler *holds* closure
signals rather than guessing, which is correct degraded behaviour and also
means no task ever reaches completed.

It accepts most, rejects some with reasons (which is what gives the
calibration panel anything to say), leaves a few undecided, and shares
boards between people at both access levels -- a demo that only ever shows
`full_trace` never shows the redaction working.

Then check it before anyone is watching:

```zsh
python3 scripts/demo_preflight.py
```

which asserts the two ledgers actually disagree, the synthetic board is
not a shell, both access levels are present, and every panel answers under
both profiles. Each of those failed at least once while this was built,
and the two worst -- an empty review queue and a board with nothing
completed -- looked fine in the seeding script's own summary. It is not
a new write path: accepts go through `cli/review.py`'s `apply_accept()`,
which goes through `apply_task_event()`, which demands a decision. Every
decision it files carries a note identifying it as seeded. It refuses to
run against a database named like the live ledger, because it writes
decisions in other people's names — fine on invented work, not on theirs.

## Making more synthetic material

```zsh
python3 testdata/synth_meetings.py \
    --template ~/task-ledger-inbox/processed/<any-real-window>.json \
    --out ~/task-ledger-synthetic-inbox --meetings 6 --turns 14 --seed 7
```

The template supplies **the participant directory and the window format,
and nothing else** — no subject, no sentence, no task. That is what lets
the generated meetings exercise the parts of the pipeline that depend on
real identity data (surname-only speakers, first-name aliases, people who
never speak in a window) while containing nothing about anyone's actual
work. The generator refuses to write inside the repository, because its
output carries that directory.

Seeds are deterministic: the same seed gives the same meetings, so an eval
result is reproducible.

## Why the cast has to be real

Attribution — the defect the first live run exposed, at 22 of 27
rejections — runs entirely through the directory. Invented names exercise
none of it. Real names with invented work exercise all of it.

## Golden labels

Every generated window ships a sidecar under `golden/` naming, for each
planted claim, who should own it and why:

| rule | meaning |
|---|---|
| `speaker` | the speaker committed themselves; the owner is the speaker |
| `target` | an explicit assignment to a named person |
| `none` | the sentence names nobody, and leaving it unowned is the correct answer |

That third rule is the one the live data showed mattering most: 15 of 22
rejected proposals came from sentences that named nobody, so a fix that
attributes them to whoever spoke would score *worse*, not better. The
scorer counts a correct abstention as a success and a wrongly-attributed
claim as its own category.

```zsh
python3 eval/run_attribution_eval.py golden \
    --dsn "$DB_DSN" --golden ~/task-ledger-synthetic-inbox/golden --show-misses
```

## The whole loop

1. `synth_meetings.py` — generate material with known answers
2. `cadence_tick.py` — run the real pipeline over it
3. `run_attribution_eval.py golden` — score the extractor against the plant
4. change something in extraction or the guards
5. drop the synthetic database, repeat from 2 with the same seed

Step 5 matters: content-hash ids make re-extraction of the *same* window a
no-op, so a fresh database is how a change gets measured rather than
skipped.

The real-rejection worksheet (`EVAL.md`) stays the ground truth that
counts, because it is made of sentences people actually said. The golden
loop is the one you can run on every change without a labelling pass.
