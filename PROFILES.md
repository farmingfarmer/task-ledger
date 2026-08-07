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
