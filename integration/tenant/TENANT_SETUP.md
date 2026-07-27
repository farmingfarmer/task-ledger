# Home-Tenant Graph Proof (Mac)

Goal: real Microsoft messages flowing through the real pipeline --
notification -> webhook -> window -> inbox file -> extraction -> board --
on a tenant where YOU are the admin. At the end, the sentence for Keck
changes from "the integration is designed" to "the integration runs".

Files here: `graph_auth.py` (both OAuth flows, stdlib), `webhook_host.py`
(handshake + clientState-verified notifications -> cadence inbox files),
`subscribe_mail.py` (subscription lifecycle), `pull_chats.py` (delegated
Teams chats, no protected APIs), `test_tenant.py` (7 offline checks).

## 1. Get a tenant

Two paths, check in this order:

- **M365 Developer Program** (developer.microsoft.com/microsoft-365):
  sign in and look for "Set up E5 subscription" on the dashboard. As of
  2025-26 eligibility is RESTRICTED -- Visual Studio Pro/Enterprise
  subscribers, ISV Success / partner-program members qualify; most
  individuals no longer do. Two minutes to check; do not fight it.
- **M365 Business Standard trial** (microsoft.com/microsoft-365/business):
  one month free, credit card, cancel-anytime. You become admin of a
  fresh `*.onmicrosoft.com` tenant with Teams and Exchange. This is the
  reliable path. Create 2-3 extra users (free within the trial seats) so
  chats and assignments have real counterparties.

Either way you end with an admin account like
`you@<something>.onmicrosoft.com`.

## 2. Register the app (Entra portal, ~5 minutes)

entra.microsoft.com, signed in as the tenant admin:

1. **App registrations -> New registration.** Name `task-ledger-dev`,
   single tenant, no redirect URI. Note the **Application (client) ID**
   and **Directory (tenant) ID** from the overview page.
2. **Certificates & secrets -> New client secret.** Copy the VALUE
   immediately (shown once).
3. **API permissions -> Add -> Microsoft Graph:**
   - Application: `Mail.Read` (mail subscription path)
   - Delegated: `Chat.Read`, `User.Read` (pull_chats path)
   Then **Grant admin consent** (the button on the same page -- you are
   the admin; this is the click that requires a committee at Keck).
4. **Authentication -> Advanced settings -> Allow public client flows:
   Yes** (enables device-code sign-in for pull_chats).

Then on the Mac:

```zsh
mkdir -p ~/.task-ledger
cat > ~/.task-ledger/tenant.json << 'EOF'
{
  "tenant_id": "PASTE-TENANT-ID",
  "client_id": "PASTE-CLIENT-ID",
  "client_secret": "PASTE-SECRET-VALUE"
}
EOF
chmod 600 ~/.task-ledger/tenant.json
```

## 3. First light: delegated chats (no webhook needed)

Post a few messages in the tenant's Teams (a chat between your admin and
a test user; include something task-shaped: "can you take the deploy
checklist by Friday?"). Then:

```zsh
cd ~/dev/task-ledger/integration/tenant
python3 pull_chats.py --hours 24
```

First run prints the device-code sign-in (URL + code, sign in as the
admin). Result: real chat windows in `~/task-ledger-inbox` -- and then
the pipeline you already trust takes over:

```zsh
export DB_DSN="host=localhost port=5432 dbname=taskintel user=$(whoami)"
TL_USER=aad-alex TL_NO_NOTIFY=1 python3 ../../cadence/cadence_tick.py
python3 ../../cli/review.py --as <the-ref-for-your-user> --dsn "$DB_DSN"
```

Real Teams messages -> mistral extraction -> proposals -> your decision
-> provenance-backed fact. That alone is most of the proof.

## 4. The webhook loop: live mail notifications

```zsh
python3 webhook_host.py --port 8443
# second terminal -- either tunnel works, no admin required:
brew install ngrok && ngrok http 8443        # note the https URL
# or: devtunnel host -p 8443 --allow-anonymous
# third terminal:
python3 subscribe_mail.py create \
    --url https://<tunnel-host>/webhook \
    --user you@<tenant>.onmicrosoft.com
```

Creation succeeds only if the handshake round-trips (Graph POSTs the
validationToken through the tunnel and the host echoes it -- watch it in
the webhook terminal). Then send that user an email from a test account.
Within seconds: notification arrives, clientState verified, message
fetched by pointer, window opens; ~60s of quiet later the window flushes
to an inbox file, and the cadence tick extracts it. `/healthz` shows the
batcher state; `subscribe_mail.py renew` before each session (mail subs
live ~3 days); `delete` when done.

## 5. What stays gated, and the form to file NOW

App-only subscriptions to Teams **channel and chat messages** are
Microsoft protected APIs even on your own tenant. The request form
(search "Microsoft Teams protected APIs request form") takes minutes to
fill and weeks to process -- file it for the dev tenant today so the
approval exists when you want the full Teams subscription demo. The
architecture is indifferent: `graph_ingestion.py`'s getAllMessages
subscriptions light up on the same webhook host when approval lands.

## 6. Security posture of this kit

Secrets live in `~/.task-ledger/*.json` chmod 600, never in the repo
(`.gitignore` covers nothing here because nothing lands in the tree);
notifications are pointer-only, content fetched by authenticated call --
the evidence philosophy applied to transport; clientState is minted per
subscription and verified per notification, spoofs logged and dropped;
tokens never print. The same properties become bullet points in the Keck
one-pager.
