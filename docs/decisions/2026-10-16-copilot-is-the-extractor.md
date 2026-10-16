# Copilot is the extractor (2026-10-16)

## The decision

Task-ledger ships with ONE extraction mechanism: Microsoft Copilot's
meeting summaries, ingested as structured work claims. The local-model
extraction arm is retired from the product. `tl kit` now defaults to
`--ledger copilot`; the blind two-arm kit remains buildable with
`--ledger both` but is no longer what anyone receives.

## The evidence

A blind A/B pilot (salt `task-ledger-blind-v1`, arms randomised per
person, labels never sent to the browser). One reviewer completed both
arms: Jason Tjia, viewer `aad-jason-tjia`, whose mapping was
**Ledger A = local extractor, Ledger B = Copilot**.

- Ledger A (local): "junk cards -- most of them"; worst card was
  meeting mechanics ("share screen / open the board").
- Ledger B (Copilot): "No bad cards in ledger B"; 3/3 tasks
  recognised with correct attribution.

Jason's stated final position: go with the Ledger B mechanism. The
operator concurred. One reviewer is a small n; the margin was not
small, and it matched the operator's own side-by-side reading.

## What the losing arm still contributes

The transcripts pipeline survives, demoted from extractor to
**evidence substrate**: `tl transcripts` keeps landing utterance
windows in the inbox, and quote anchoring
(`extraction/quote_anchor.py`) matches Copilot's quoted sentences
against them. An anchored claim earns a checkable receipt, the real
speaker, and confidence above the 0.75 unverified cap -- which is what
lets a Copilot completion claim propose that work is finished. Without
transcripts on disk, Copilot claims still land but stay capped and
cannot close work. So the ingest order is: transcripts first, claims
second.

## What was never in question

Matching, corroboration, cross-meeting memory, the human accept gate,
receipts, and routing are the product and are extractor-independent.
Copilot sees one meeting and forgets; the ledger is everything that
happens after extraction.

## Standing constraints this does not change

- Real meeting content never enters the repo or any cloud. Claims
  files, transcripts, and status reports stay gitignored.
- A kit's corpus is the operator's meeting attendance. Meetings that
  are not all-team (1:1s, per-person assignments) must NOT enter the
  shared dump every kit carries -- coverage of those waits on
  per-person ledgers via the Graph API.
- Work assigned outside meetings (sprint board, chat) is invisible to
  any transcript-based extractor; status-report ingestion
  (`tl statusreport`) is the interim channel, Graph the permanent one.
