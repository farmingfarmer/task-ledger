# Attribution eval: real rejections as a test set

`testdata/` measures extraction against synthetic corpora, which is how the
prompt got from 76% to 97% recall. What synthetic text cannot produce is the
sentence that actually defeats attribution on real communication. The first
live run produced 27 rejections, and **22 of them were `wrong_owner`** — so
the failures are already labelled by class, on real language, by the person
who knew the answer.

```zsh
# 1. build the worksheet — writes OUTSIDE the repo, and refuses otherwise
python3 eval/run_attribution_eval.py build --dsn "$DB_DSN" \
    --out ~/task-ledger-eval/attribution.json

# 2. fill in correct_owner_ref on each case (about ten minutes)

# 3. measure
python3 eval/run_attribution_eval.py score \
    --worksheet ~/task-ledger-eval/attribution.json
```

## What the ledger records, and what it doesn't

**Recorded:** that a proposal was rejected and why, from an enumerated menu.
**Not recorded:** who the right owner was. The menu asks *why*, not *who*, so
no amount of querying produces ground truth — hence a worksheet and one human
pass. That gap is itself a finding: a `reject_note` prompt on `wrong_owner`
would make the next eval build itself.

## Where the text comes from

Not from the ledger. Evidence there is a pointer plus a hash, by design, so
the builder joins `resource_id` back to the **processed inbox windows** to
recover the sentence and its speaker. A worksheet therefore contains real
colleague speech, and `assert_outside_repo()` refuses to write one inside the
working tree, where `git add -A` would eventually sweep it up.

## What the first build found

Before a single label was filled in:

| | |
|---|---|
| cases | 22 |
| with a recorded speaker | 22 |
| **extractor emitted no owner at all** | **18** |
| self-referential sentence, yet unowned | 11 |
| extractor named someone other than the speaker | 19 |

The dominant failure is not *wrong person* — it is **no person**. The model
returned an empty `owner_candidates` in 18 of 22 (confirmed against
`signals.raw`, so the resolver did not drop them; they were never emitted),
while the speaker was recorded in the window the whole time.

## The measurement that decides the fix

`score` reports how many labelled cases the **speaker's own identity** would
resolve — and, just as important, how many it would *break*: a sentence like
"Sam should own the migration" is spoken by Robin and belongs to Sam, so a
naive default-to-speaker rule gets it wrong. Both directions are counted
(`speaker_would_fix` vs `speaker_is_not_owner`), because the safe fix is
narrow: *when no owner was extracted and the sentence is first-person, use
the speaker* — not *always use the speaker*.

The verdict line names which layer to work in: wire `author_ref` if the
speaker resolves most of them, prompt-and-guard work if it doesn't.

## The half that runs without a human

The worksheet above is ground truth, and it costs a labelling pass every
time. `golden` is the half that can run on every change: synthetic meetings
(`testdata/synth_meetings.py`) whose owner was decided *before the model saw
the sentence*, shipped as a sidecar per window.

    python3 eval/run_attribution_eval.py golden \
        --dsn "<synthetic dsn>" \
        --golden ~/task-ledger-synthetic-inbox/golden --show-misses

The scoring rule that matters: **a sentence naming nobody counts as correct
when left unowned**, and attributing it is its own category
(`wrongly_owned`). Without that column a default-to-speaker change scores
*better* while making the live failure worse — 15 of the 22 real rejections
were exactly that shape.

### Baseline — 6 windows, 77 planted claims, after the routing fix and guard

| | |
|---|---|
| reached extraction | 75 / 77 |
| owner correct | 43 |
| correctly left unowned | 20 |
| owner **wrong person** | 1 |
| owner missing | 7 |
| **wrongly attributed** (named nobody) | **6** |
| **accuracy** | **81.8%** |

Wrong-person is 1 in 77 — the failure that started this work is essentially
gone. What remains splits in two, and neither is the guard: all 6 wrong
attributions carry `basis="inferred"` at 0.8, i.e. the model's own judgement
on impersonal sentences ("X is stuck waiting on the platform team", "let's
get X scoped"). The guard fired 9 times (`self_reference`, 0.75) and appears
in none of them.

The 7 misses are a different shape — a speaker reporting that *somebody
else* finished something, a speaker reporting their own blocker, and one
sentence that addresses a person by name and asks them to take the work,
which should have been the easiest case in the set. Those are prompt work,
deliberately out of scope until this number can be watched across a change.

(The examples here name no one on purpose. The generated sentences are
invented but the speakers are the real directory, so a quoted miss would
carry a colleague's name into the repository — the same reason the
worksheet is written outside it.)

## Validation

`test_eval.py` — 27 checks, no database, invented fixtures: case assembly,
first-person detection, missing windows reported rather than dropped, the
scorer's two directions, the golden scorer's unowned-is-correct rule, a
claim that produced no signal at all being reported with its text rather
than silently counted, and the repo-write guard.
