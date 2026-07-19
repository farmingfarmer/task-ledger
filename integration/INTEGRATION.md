# Integration: Graph Ingestion, Bot Host, Dashboard

The three pieces that connect the validated core (contracts, matching policy, review loop, storage) to the real world. `ingestion/graph_ingestion.py`, `bot/bot_host.py`, `dashboard/dashboard_api.py` + `dashboard.html`; `tests/test_integration.py` runs the ingestion and bot suites against the real orchestrator (23 checks); the dashboard is validated live against the Postgres store from `storage/`.

## 1. Graph webhook subscriptions -> extraction queue

**SubscriptionManager** creates the tenant-level Teams subscriptions (`/teams/getAllMessages`, `/chats/getAllMessages`) plus per-user mail subscriptions, each with its own random `clientState` — the only authentication Graph gives plain notifications, so it's per-subscription and verified on every delivery. Lifetimes differ by resource (chatMessage ~3 days, mail ~7), and `renew_due()` renews at half-life on a timer, so a single missed timer run never expires a subscription. Meeting transcripts have **no** change notifications; the manager emits a poll plan for meetings that ended since the last sweep, and the extraction worker fetches those via the Meeting Insights surface.

**WebhookHandler** does exactly four things and nothing slow: echoes the validation handshake token, drops (and counts) notifications with a wrong `clientState`, handles lifecycle events (`reauthorizationRequired` -> renew, `subscriptionRemoved` -> drop the record so the next ensure pass recreates it), and hands change notifications to the batcher. It returns 202 immediately — Graph requires sub-3-second responses, so all real work is a queue append downstream.

**WindowBatcher** is the piece that makes extraction affordable and good. Teams emits one notification per message; extracting per message wastes LLM calls and — worse — loses conversational context, which is exactly what the extractor needs to attribute owners and resolve "it". The batcher windows notifications per conversation container (team/channel, chat, mailbox — derived from the resource path, never the tenant firehose) and flushes on a quiet-period debounce (default 180s) with a max-age cap (30 min) so long-running threads still flow at bounded latency. The flushed job is precisely an ExtractionBatch window from `CONTRACTS.md`: source ref, time bounds, resource ids. **Content never enters the queue** — the extraction worker fetches message bodies at processing time under its own permissions; pointer-not-copy discipline starts at ingestion, not at storage.

## 2. Bot Framework host

**ConversationReferenceStore** captures the `conversationReference` when a user installs the bot (`conversationUpdate`/`membersAdded`, skipping the bot's own id) and refreshes it on any 1:1 message. Persist this table in production — losing references means you cannot proactively message a user until they talk to the bot again.

**TeamsTransport** implements the orchestrator's transport duck type over two injected callables (`connector.send`, `connector.update` — raw Bot Connector REST or the botbuilder SDK, either fits). `deliver_card` renders through `review_card.py` and remembers `activity_id -> (user, packet)`; `refresh_card` re-renders for the packet's current state: undecided proposals only while open, receipt/superseded/expired stubs at terminal states. The chat history therefore never shows a live-looking card for a dead packet.

**InvokeRouter** is where the security posture lives, and the tests attack it directly:

- `decided_by` is stamped from `activity.from.aadObjectId` — the identity authenticated by the Bot Framework JWT on the endpoint — **never** from card data. The test sends a card payload claiming `decided_by: mallory` from alex's authenticated activity and asserts the recorded decision says alex.
- A non-owner invoking against someone else's packet gets a refusal and zero board writes.
- Bulk verbs expand server-side into individual DecisionRecords (audit log never contains compound entries), and `accept_unconflicted` skips conflicted proposals — the test confirms the conflicted completion survives bulk accept and only falls to an explicit `defer_all`.
- Reject reasons are read from the ShowCard input keyed by `reject_reason_input_id`, defaulting to `other` — the calibration pipeline's food supply.

Every invoke returns the re-rendered card as the invoke response, so the card in the thread is always current without a second round trip. The endpoint glue (aiohttp or Azure Functions) is ~20 lines: authenticate the JWT, parse the Activity, route `conversationUpdate` to the ref store and `adaptiveCard/action` invokes to the router.

## 3. Dashboard

**API** (`dashboard_api.py`): Flask over psycopg2, one endpoint per query path — Q5 team rollup, Q1 board, Q2 history, Q3 provenance, Q4 blocked-on. Connections are set `READ ONLY` at the session level, so even a bug in this layer cannot mutate the board; all writes stay behind `apply_task_event` via the decision flow. Validated live against the seeded Postgres store: all five endpoints return correct shapes, including the full provenance walk from a blocker event down to `msg-11` with its integrity hash.

**Front end** (`dashboard.html`, single file, no build step): a two-pane workbench. Left: team rollup chips (Q5, click to switch boards), the board table (Q1 — status as quiet left-border ticks, overdue and blocker flags), and a "waiting on this board" strip (Q4). Right: the drill panel.

The signature element is the **provenance rail**: a task's history renders as a version-numbered ledger (the numbers are real — they're `board_version`, so the numbering encodes actual sequence, not decoration), each event tagged by actor as *typed by hand*, *confirmed review*, or *system*. One color — evidence amber — is reserved on the entire page for exactly one meaning: *this links to a source*. "Trace to source" expands an event into its signals and evidence chips (source type, resource id, integrity hash in the tooltip); in production the chips resolve `resource_id` to Graph deep links at click time, under the viewer's own permissions. Nothing quoted, everything traceable — the UI is the argument for why people should trust the board.

Type is Archivo (display and body) with IBM Plex Mono for ids, versions, and dates; palette is cool mist over white panels with pine/brick/slate status hues. Keyboard focus is visible, rows are focusable and Enter-activatable, reduced motion is respected, and the layout stacks below 960px.

## Production wiring checklist

- Host the webhook handler behind HTTPS with a stable URL before creating subscriptions (Graph validates at create time).
- Application permissions: `ChannelMessage.Read.All`, `Chat.Read.All`, `Mail.Read` (protected APIs — the Teams message subscriptions require Microsoft approval), plus `OnlineMeetings`/Meeting Insights scopes for the transcript poller.
- The extraction worker fetches content by resource id at processing time; give it its own identity and audit its reads.
- Bot endpoint must validate the Bot Framework JWT before the router runs — the router's identity guarantees are only as good as the endpoint's authentication.
- Swap the dashboard's evidence chips from placeholders to Graph `webUrl` resolution, and put the API behind the same AAD auth as everything else, filtering Q1/Q2/Q3 by what the viewer is permitted to see.
