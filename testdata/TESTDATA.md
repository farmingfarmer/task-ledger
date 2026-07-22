# Test Data: Synthetic Messy Comms + Extraction Scoring

`testdata/` answers the question the clean fixture couldn't: **how does
extract-v1.0 hold up against communication that looks like a real
workday?** No Mnemo, no real data -- a seeded scenario engine
(`synthesize.py`) and a grader (`score_extraction.py`) that runs the REAL
extraction worker (real prompts, salvage, hallucination guards) and scores
it against ground truth.

## The corpus

Each simulated day produces four windows in cadence-inbox format, one per
channel style Graph would deliver, each rendered with channel-appropriate
mess:

- **standup transcript** -- fillers, interruptions, crosstalk
- **team channel** -- lowercase, typos, eod/tmrw abbreviations, distractor
  chatter interleaved mid-thread
- **1:1 chat** -- terse, context assumed
- **email** -- greetings, signatures, quoted history under the content

Guaranteed hard cases every day (never left to the dice): a **negation
trap** ("we are NOT doing X this quarter", "no longer blocked" -- the trap
is extracting the affirmative), a **third-party completion claim**, a
**vague deadline**, and **cross-channel duplicate sightings** of the same
task. Name variants (Alexander/Alex/AM) exercise the directory throughout.
Deterministic per `--seed`; `golden.json` carries the labels, including
which readings of each trap are acceptable.

## The scores and what they mean downstream

- **recall** -- missed signals are work the board never hears about
- **precision / fabrications** -- invented signals waste review attention
- **attribution** -- owner + basis drive the assignment ladder and
  completion gates; wrong basis here corrupts gating there
- **vague-date safety** -- "Friday-ish" must arrive vague or null, never as
  an invented date (the card shows raw text either way, but invented dates
  cost review trust)
- **negation trap hits** -- the worst failure class: parked work revived,
  unblocks read as blocks
- Duplicate sightings are deliberately NOT penalized: extraction should
  report every sighting; dedup is the reconciler's job.

The harness itself is verified honest: a cheating LLM built from the
golden labels scores 100% on every axis; an empty LLM scores 0% recall.
Whatever mistral scores between those poles is real.

## Running it

```zsh
cd testdata
python3 synthesize.py --seed 7 --days 3 --out corpus
python3 score_extraction.py --corpus corpus --model mistral-small3.1
```

One extraction call per window (a 3-day corpus is 12 calls); per-window
detail lands in `corpus/score_report.json`, trap hits printed with the
offending extraction. Different seeds are different days -- regression-test
prompt changes on a fixed seed, explore variance across several.

## Reading results into action

Low recall on a channel style -> prompt needs examples of that register.
Trap hits -> negation guidance needs strengthening (and the specific
trap text is right there to paste into the prompt as a negative example).
Attribution misses on variants -> directory or attribution spec work.
Then: bump PROMPT_VERSION, re-run the same seed, diff the reports --
that's the calibration loop running on synthetic time instead of waiting
weeks for real usage. Fixture format matches synthkit's eval shape by
design; graduating these scenarios into a tiered synthkit campaign is a
natural next step once the failure patterns are known.

## Full-loop option

The windows are valid cadence inbox files. To watch the whole system
digest a synthetic day -- extraction, reconcile, supersede, review CLI --
use a scratch database so synthetic tasks never touch your working board:

```zsh
createdb taskintel_synth
psql -d taskintel_synth -f ../storage/schema.sql
psql -d taskintel_synth -f ../access/access_control.sql
cp corpus/window-0[1-4]* ~/task-ledger-inbox/
DB_DSN="host=localhost port=5432 dbname=taskintel_synth user=$(whoami)" \
  TL_USER=aad-alex TL_NO_NOTIFY=1 python3 ../cadence/cadence_tick.py
```
