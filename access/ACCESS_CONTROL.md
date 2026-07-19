# Access Control: Who Sees Whose Work

Adds the visibility layer to the design set. `access_control.sql` is the migration (apply after `schema.sql`), `validate_access.sql` its assertion suite; `dashboard_api.py` and `dashboard.html` in `integration/dashboard/` are the viewer-aware replacements. All validated by running: 8 storage assertions pass, and the API was exercised live as a stakeholder, a teammate, a stranger, and an anonymous request.

## 1. The model

Two **scopes**, two **levels**, one implicit rule:

| | `progress` | `full_trace` |
|---|---|---|
| **`user` scope** (a whole board) | follow a person's task states | teammate view: everything, incl. provenance |
| **`project` scope** (one workstream, any owner) | *the stakeholder case*: follow one project's progress across everyone working on it, and nothing else those people do | embedded collaborator on one project |

The implicit rule: **owners always have `full_trace` on their own tasks** — no grant rows, nothing to misconfigure, no way to lock yourself out.

The scenario you described falls out directly: alex creates `proj-drift`, tags `task-88` into it, and grants vic `progress` on the project. Vic now sees the drift work — including priya's `task-99`, because project scope crosses owners — and cannot see `task-70`, alex's other work, or that it even exists.

**Levels are about communication-derived content, not task data.** `progress` shows the history skeleton: what changed, when, and by which *class* of actor (typed by hand / confirmed review / system). It redacts everything that originated in internal communications the stakeholder wasn't part of: proposal rationales ("in Tuesday standup you said…"), decider identity, model confidence, blocker wording, aliases, and all provenance. `full_trace` opens the whole rail down to evidence pointers — and even then, clicking an evidence chip resolves the Graph resource under the *viewer's own* M365 permissions, so full_trace on the board never overrides message-level access control. The two layers compose: this system governs what the board shows; Graph governs what the sources show.

## 2. Grant authority and audit

Who may share what is enforced in `grant_access()`, the only write path:

- **Your board** — only you. Nobody, including a manager, shares someone's board on their behalf through this system; that's a deliberate line, because a transparency tool that lets third parties expose people's work is how these tools become surveillance.
- **A project** — only the project owner, and only project-scoped visibility.

Grants are **append-only events** (`granted` / `revoked`), consistent with everything else in the store: current access is a latest-event-wins view, upgrades are just newer events (validated: progress → full_trace takes effect immediately), revocation removes visibility instantly while the full history of who shared what with whom, when, and why (the `note` field) remains queryable forever. When someone asks "why can vic see this?", the answer is a row, not a shrug.

Grant management deliberately does **not** go through the read-only dashboard API. The natural production surface is the bot — a `/share project:drift with:vic level:progress` command routes to `grant_access()` with the authenticated Teams identity as `granted_by`, reusing the exact identity discipline the InvokeRouter already enforces.

## 3. Enforcement architecture

One function, `visible_tasks(viewer)`, resolves the entire policy: owner > full_trace grant > progress grant, across both scopes. **Every** task-reading endpoint joins through it; nothing reads `tasks_current` for another user directly. That single-choke-point shape is the whole security argument: a policy change is one SQL function, and there is no second code path for a future endpoint to forget. The API additionally stays session-`READ ONLY`, so the visibility layer sits on top of a layer that structurally cannot write.

Details that matter and are asserted in the validation:

- **No count leakage.** The team rollup aggregates over *visible* tasks only. Vic's view of alex shows one blocked task, not "1 of 2" — the existence of unshared work is itself unshared.
- **Provenance is 403 below full_trace**, checked against the event's task at request time, not inferred from what the client renders.
- **The dependency view (Q4) is owner-only** unless the whole board is shared at full_trace — "who is blocked on me" reveals other people's tasks and is the easiest place to leak sideways.
- **Rebuild honesty still holds**: `project_assigned` is a board event like any other, `rebuild_board()` replays it, and the suite asserts project membership survives reconstruction.
- **403s and 401s are enforced server-side**; the front end's redaction styling is presentation, never the control.

## 4. What the stakeholder actually experiences

The dashboard gained a "Viewing as" switcher (demo stand-in for the signed-in AAD identity), project badges on tasks, an "Access you have granted" panel for owners, and level-aware drill-down. At progress level the history rail still tells a real story — created, status changes, deadline moves, blocked, cleared, versions and dates intact — prefaced by a plain note: *"Shared at progress level: you can follow what changed and when. Discussion detail and sources stay with the team."* No amber appears anywhere in their view, keeping the page's one color contract honest: amber means "you can reach the source," and they can't.

## 5. Knobs left deliberately simple

Three extensions the model supports without schema change, left out until needed: **group grantees** (grant to an AAD group id; resolution adds one membership join in `visible_tasks`), **expiring grants** (an `expires_at` on the grant event and one predicate in the view — useful for vendor engagements), and a **per-project default** (auto-grant progress to a stakeholder list on task creation). Each is a policy decision to make with the org first; the storage shape is already right for all three.
