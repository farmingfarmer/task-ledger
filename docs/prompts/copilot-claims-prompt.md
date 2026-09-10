# Asking Copilot for structured work claims

Paste the block below to Copilot with a meeting open (or with the
transcript pasted in). Save what it returns as a `.json` file, then:

```
python scripts\copilot_claims_ingest.py "<that file>" --dry-run --label "scrum-2026-08-31"
```

Read the dry run before loading anything. To actually load it, drop
`--dry-run` and add `--dsn "%DB_DSN%"` — plus `--bootstrap` on the first
import into an empty ledger (see *Before you read the results* below).

The vocabularies below are **parsed, not read**. A value outside the
listed set silently degrades to a weaker claim type, so the wording of
the prompt matters more than it looks.

---

## The prompt

Analyse this meeting transcript and output **only** a JSON array of work
claims. No prose before or after.

One object per distinct piece of work per person. Use exactly these
fields and exactly these vocabularies:

```json
[
  {
    "person": "Firstname Lastname",
    "task": "short imperative description of the work",
    "project": "the larger project or workstream this belongs to, or null",
    "status": "in_progress | open | backlog | waiting | blocked | done | completed | cancelled",
    "claim_type": "explicit_commitment | progress_update | blocker | blocker_resolved | completion | mention",
    "progress": "what was reported about how far along it is, or null",
    "blockers": ["what is in the way, if anything"],
    "next_action": "the stated next step, or null",
    "estimate": "the effort estimate EXACTLY as spoken — '3 points', '2 SP', 'half a day' — or null",
    "confidence": "high | medium | low",
    "evidence_strength": "transcript_supported",
    "sources": ["<meeting title and date>"],
    "quote": "the exact sentence from the transcript this claim came from",
    "said_by": "Firstname Lastname who spoke that sentence",
    "timestamp": "HH:MM of that sentence"
  }
]
```

Rules that matter:

- **`person` is who will DO the work; `said_by` is who spoke.** They are
  often different — "Priya, can you take the IAM role?" has person Priya
  and said_by whoever asked. Never merge them.
- **`quote` must be verbatim** from the transcript, not paraphrased. If
  you cannot quote it, do not emit the claim.
- **`project` is where this belongs**: if someone says "count events per
  day" in a conversation about stroke prediction, the project is the
  stroke prediction work. Use the same project string across claims that
  belong together. Null if genuinely standalone.
- **`confidence` is about the claim, not the work**: high if someone
  plainly said it, low if you are inferring.
- **`estimate` is the words, never a conversion**: if someone said
  "three points", write "3 points" — do not turn points into hours or
  days. The ledger owns the conversion rate; your job is what was said.
- Full names, spelled identically everywhere, matching the participants
  list.
- **Do not emit claims for**: opinions, questions with no commitment,
  hypotheticals, pleasantries, scheduling the next meeting, or anything
  that happens *during* the meeting and is finished when it ends —
  sharing a screen, pulling up a document, walking through a chart.

---

## Running the comparison

Both of these are read-only and can be run as often as you like.

```
tl.bat quickscan "C:\path\to\that-meeting-transcript.txt"
python scripts\copilot_claims_ingest.py "C:\path\to\copilot-claims.json" --dry-run --label "scrum-2026-08-31"
```

Put the two outputs side by side and count four things per side:

| | task-ledger | Copilot |
|---|---|---|
| **caught** — real work found | | |
| **invented** — not actually work | | |
| **misattributed** — wrong owner | | |
| **missed** — work you know was discussed | | |

Fill that in for one meeting you sat in and the extraction question
stops being an argument.

---

## Before you read the results

Three things, so the comparison is fair rather than flattering to either
side.

**Copilot's claims are confidence-capped at 0.75 and every owner basis
is marked `inferred`** — deliberately, because its citations cannot be
checked. That cap sits below the floors for complete and cancel, so an
unverifiable claim is structurally incapable of proposing that work is
finished. Better comprehension does not buy it the right to close
things.

**First import into an empty ledger needs `--bootstrap`.** A Copilot
export is mostly progress and blockers on work already under way, and
those need a target task to attach to. Without the flag they are
consumed with nothing to land on and twenty claims yield one proposal.

**`quote` is what earns a claim its receipt.** When the sentence you
quote is found in a transcript this system already holds, the claim
stops being an `assistant_claim` and cites that utterance by id and
content hash, carries the speaker the transcript records rather than the
one you named, and is no longer confidence-capped — which is what lets a
verified completion claim propose that work is finished. Quote verbatim
or omit the claim: an invented sentence matches nothing and is refused,
and it costs you the receipt, not just the wording.

**`project` becomes the parent.** It rides to the review card as a name
— "Files under *Intake Platform*" — and the person accepting is what
files it, under their task of that name or a new one if they have none.
So use the SAME string across claims that belong together: identical
strings land under one parent, and two spellings make two. Null is
better than a guess; work filed under the wrong parent stops appearing
where anyone looks for it.

---

## What this does and does not decide

It decides **extraction**: who is better at turning speech into typed,
attributed claims.

It does not touch what the ledger does with them — matching against work
already tracked, corroborating across meetings, routing to the right
person, holding the human gate, keeping receipts. Copilot cannot do any
of that, because it sees one meeting and forgets. Whichever way this
comparison goes, that half stays.
