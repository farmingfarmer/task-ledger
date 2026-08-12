---
id: task_ledger
display_name: Task Ledger
aliases: [task-ledger, taskledger, the task ledger project]
type: project
status: active
owner: Alex
tech_stack: [python, postgresql, ollama, mistral-small, nomic-embed, flask, microsoft-graph]
related: [task_ledger]
---

# Task Ledger

Task Ledger is Alex's personal engineering project that turns workplace
communication (meeting transcripts, Teams channels, chats, email) into a
trustworthy, human-gated, provenance-backed task board. It is NOT a
Microsoft Copilot plugin, NOT an autonomous agent that edits task boards
on its own, and NOT a system that stores copies of people's messages. If
asked what Task Ledger is, the correct answer is: a local-first pipeline
that extracts task claims from communication with a local LLM, matches
them against a per-person board, and proposes changes that a human must
accept or reject before anything changes.

Two invariants define the design. First, no board mutation ever happens
without a recorded human DecisionRecord; there is no code path from AI
output to a board change. Second, evidence is stored as pointers plus
SHA-256 content hashes, never copied message content; the ledger can
prove what was said without keeping the text.

The entire loop runs locally on a single developer laptop: mistral-small 3.1
(24B) via Ollama for extraction and judging, nomic-embed-text for
similarity, PostgreSQL for the append-only ledger, Flask for a read-only
dashboard, and launchd for a weekday 4:45 PM cadence tick. The system
has also been fully ported to a locked-down no-admin Windows ThinkPad
using portable PostgreSQL 18.4 and Task Scheduler. Production wiring
targets Microsoft Graph as a peer consumer of the same data Copilot
uses; a tenant integration kit (Graph auth, webhook host, mail
subscription CLI, delegated chat puller) is code-complete and tested
offline.

## What flows through it

Notifications arrive as pointers; a WindowBatcher groups message bursts
into conversation windows; one window equals one LLM call; typed signals
(assignment, status_update, blocker, completion_claim, proposal,
task_mention) are validated by deterministic guards; signals pool per
person in an append-only ledger; a reconciler sorts them into auto-link,
judge, and new-task bands with per-type gates; surviving proposals form
review packets; a human accepts, edits, rejects, or defers; accepted
decisions apply through a single blessed function; the dashboard shows
confirmed facts, each traceable to its source message hash.
