# Noted

A task manager for agents. It queues work, hands each task to exactly one
executor, survives the death of any of them, and shows who is doing what.

The core is a single FastAPI process over SQLite. An MCP server comes up with
it on the same port, so an agent can connect right after `up -d`. People look
at the web dashboard, and all of it runs through the same HTTP API.

```
agents ──────── MCP over HTTP (/mcp) ───────>┌──────────────────────────────┐
                                             │      FastAPI + SQLite        │
agents ─MCP(stdio)─> adapter ──HTTP─────────>│ API · MCP · SSE · dashboard  │
                                             │                              │
browser ──── static + /api + SSE ───────────>└──────────────────────────────┘
```

## Why

When several agents work on one repository they need a shared place to keep the
queue. A list in a file will not do: two of them read it at once and both start
the same task. Noted solves that, and a few things that follow from it:

- **Claiming is atomic.** However many agents take work at the same moment,
  every task goes to exactly one of them.
- **A crashed agent does not hold work.** A task belongs to a session, and the
  transport renews that session rather than the model, so a ten-minute build is
  no problem. When the process dies, everything it held is released at once,
  within ninety seconds. Not "you can see it is stuck" but "it fixed itself".
- **Failures retry with a pause.** `max_attempts` returns a task to the queue
  with a growing backoff; when attempts run out it stays `failed` — that is the
  dead letter.
- **Order of work is respected.** `depends_on` withholds a task until its
  predecessors are closed; a failed predecessor turns its waiters `blocked`.
- **Urgent goes first.** `priority` sorts the queue without breaking addressing.
- **An agent is a principal, not a string.** A key proves identity, the
  `assignee_id` follows from the key, and the project scope is enforced. Nobody
  can take another's name.
- **Safe behaviour is the default.** `set_status` without `if_status` does not
  write unconditionally: a forgotten parameter cannot break invariants in
  silence, and an unconditional write is an explicit `force`.
- **You can see who did what.** Every change is appended to a journal: who
  claimed the task, when it came back after a lease expired, how many attempts
  it took.
- **A retry breeds no duplicates.** The same idempotency key returns the
  existing task.
- **Projects stay out of each other's way.** An agent working in a project will
  not be handed another project's task.
- **A looping agent cannot flood the queue.** A per-author creation limit
  catches the breakage without getting in the way of honest decomposition.

## What it does not do

The core is a primitive: a queue where a task goes to exactly one holder,
survives that holder's death, and remembers its own history. Acceptance,
isolation of working copies, launching agents and the shape of a payload are
assembled on top and stay with whoever writes the orchestration — [RECIPES.md](RECIPES.md)
shows the primitive is enough for that.

Identity is the one exception: it cannot be built on top, so it lives in the core.

## Quick start

```bash
docker compose -f deploy/docker-compose.yml up -d
```

The dashboard is at <http://127.0.0.1:8787/>, the API docs at `/docs`, liveness
at `/healthz`.

The image builds in two stages: Node builds the frontend, the Python image takes
the finished static files — there is no Node in the runtime. The database lives
in the `noted-data` volume, so rebuilding the image does not touch it. The port
is published on the loopback only.

Without compose:

```bash
docker build -f deploy/Dockerfile -t noted .
docker run -d --name noted -p 127.0.0.1:8787:8787 -v noted-data:/data noted
```

## Connecting an agent

The MCP server is already up with the core, so nothing has to be installed or
started:

```bash
claude mcp add --transport http noted http://127.0.0.1:8787/mcp/
```

The same address, command and a ready-made config snippet are behind the
**Connect MCP** button in the dashboard header.

### Agent keys

Until the first key is issued the queue is open: anyone may post and claim. The
first key turns identity on — an upgrade breaks nothing, and strictness arrives
by an explicit act.

```bash
venv/bin/noted-keys add agent-builder --projects noted,ui-kit
venv/bin/noted-keys list
venv/bin/noted-keys revoke agent-builder
```

A key is shown once; only its hash is stored. From then on it lives in a header:

```bash
claude mcp add --transport http noted http://127.0.0.1:8787/mcp/ \
    --header "X-Noted-Token: noted_…"
```

With a key the server fills in `assignee_id` and `created_by` itself: taking
another's name is not possible. The project scope is enforced — a scoped agent
neither sees nor claims a foreign task.

For the stdio transport the key comes from `NOTED_KEY`:

```bash
claude mcp add noted -- docker run -i --rm \
    -e NOTED_API=http://host.docker.internal:8787 -e NOTED_KEY=noted_… noted noted-mcp
```

### Sessions instead of heartbeats

An LLM agent can only call tools between steps: while a ten-minute build runs it
sends no heartbeat at all. So proof of life comes from the process rather than
from the reasoning — the stdio adapter renews its session in the background, and
over HTTP the session is the `Mcp-Session-Id` the client already sends.

Inside a session a task takes no time-based lease: it is held for as long as the
agent's process lives. When it dies the session expires after `NOTED_SESSION_TTL_S`
(90 s) and every task it held returns to the queue.

That makes the executor loop short:

```
claim_task(project="noted", timeout_s=30)
  → {"ok": true, "outcome": "claimed", "task": {"id": 42, ...}}

set_status(task_id=42, status="done", result={"passed": 40})
  → {"ok": true, "outcome": "updated", ...}
```

`timeout_s > 0` is a long poll: the connection waits for a task and returns the
moment one appears. There is no need to poll the queue in a loop.

`set_status` without `if_status` checks that the task is still `in_progress`, so
a forgotten parameter breaks nothing. An unconditional write is `force=true`.

## MCP tools

| Tool | Purpose |
|---|---|
| `set_task(task, project, assignee_id, parent_id, key, created_by, priority, max_attempts, depends_on)` | post a task |
| `get_tasks(assignee_id, status, project, unscoped, unassigned, parent_id, stale_seconds, before_id, limit)` | list with filters and a cursor |
| `get_task(task_id, with_events)` | one task in full, with its result and journal |
| `set_status(task_id, status, result, if_status, assignee_id, force)` | change status and attach a result |
| `claim_task(assignee_id, project, timeout_s, lease_s)` | take a task atomically |
| `heartbeat(task_id, assignee_id, lease_s)` | extend the lease: "I am alive" |

Every answer arrives in one envelope with an `outcome` field, and an agent
decides what to do next by it:

| `outcome` | `ok` | HTTP | When |
|---|---|---|---|
| `created` · `exists` | true | 201 · 200 | the task was created · the idempotency key matched |
| `ok` · `updated` · `claimed` | true | 200 | a read · status changed · task taken |
| `empty` | true | 200 | nothing to take, the long poll ran out — not an error |
| `not_found` · `parent_not_found` | false | 404 | no such task · no such parent |
| `status_conflict` | false | 409 | `if_status` did not match, or the task is in another state |
| `not_owner` | false | 409 | the task is held by another executor |
| `stale_session` | false | 409 | another instance of the same agent holds it |
| `forbidden` | false | 403 | the project is outside the key's scope |
| `validation_error` | false | 422 | malformed input |
| `unauthorized` | false | 401 | `NOTED_TOKEN` is set and the header did not match |
| `rate_limited` | false | 429 | the author posts faster than the limit |
| `api_unavailable` | false | — | the core is not answering; returned by the adapter |

`not_found`, `status_conflict` and `empty` are normal outcomes: an agent has to
handle them rather than crash. Malformed input, a refused key and an unreachable
core are additionally marked as tool errors.

## Model

**A task** is an arbitrary JSON object plus metadata: status, project, assignee,
author, parent, idempotency key, result, and the times it was created and last
touched.

**Statuses** are a fixed set: `pending` · `in_progress` · `blocked` · `done` ·
`failed` · `cancelled`. There is no state machine; `if_status` is what controls
transitions.

**Projects** are an arbitrary string, `null` meaning "outside every project".
The scope is strict: an agent naming a project in `claim_task` gets neither
another project's task nor one with no project. Name none and it takes from
anywhere. There is no project table — the list is derived from the tasks.

**Dispatch order** is a contract: `priority` descending → addressed before the
pool → FIFO by `id`. Tasks still inside a post-failure pause, tasks with open
dependencies and tasks of other projects are never handed out. Starvation is
possible and deliberate: a stream of high-priority work can hold off the
low-priority indefinitely, and there is no priority ageing in the core.

**A lease** is taken on claim. It is renewed by `heartbeat`, and on expiry the
task returns to the shared pool (or goes to `failed` when attempts are gone).
`lease_s=0` takes none. Inside a session no lease is set at all.

**Retries**: `max_attempts` turns a failure into a return to the queue with an
exponential pause. Exhausted attempts leave the task `failed`.

**Dependencies**: `depends_on` keeps a task unclaimable until its predecessors
are `done`. A failed predecessor turns its waiters `blocked`; a successful one
unblocks them.

**The journal** appends an entry on every change: `created`, `claimed`,
`status`, `retry`, `reaped`, `dead_letter`, `blocked`, `unblocked`. Entries of
closed tasks older than `NOTED_JOURNAL_KEEP_DAYS` are dropped; the journal of a
live task is never touched.

**The event stream**: `GET /events` serves those same entries over SSE and acts
as a public contract — every event carries a number, and a client that
reconnects continues from it (`Last-Event-ID` or `?after=`) without losing what
it missed. Integrations are built on that rather than on polling.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/tasks` | create a task |
| `GET` | `/api/tasks` | list with filters |
| `GET` | `/api/tasks/{id}` | one task in full |
| `PATCH` | `/api/tasks/{id}/status` | change status |
| `POST` | `/api/tasks/claim` | take a task atomically, with long poll and lease |
| `POST` | `/api/tasks/{id}/heartbeat` | extend the lease |
| `GET` | `/api/tasks/{id}/events` | the transition journal of a task |
| `GET` | `/api/stats` | counters, plus the project and assignee lists |
| `GET` | `/events` | the SSE stream of changes |
| `POST` | `/api/sessions/renew` | session renewal by the transport |
| `POST` | `/api/login` · `/api/logout` | the dashboard door |
| `POST`/`GET` | `/mcp/` | MCP over the streamable-http transport |
| `GET` | `/healthz` | liveness |

The interactive schema is at `/docs`.

## Dashboard

React and TypeScript on [astralyx-ui](https://ui.astralyx.dev), built by Vite and
served by the core from the root. It runs on the same `/api` the agents use.

- A table of tasks with statuses, projects, assignees and a per-row action menu;
  long lists load in pages as you scroll.
- Filters by status, project, assignee and "not updated for N"; the state lives
  in the query string, so a filtered view can be shared as a link.
- Counters are computed inside the selected project, not over the whole database.
- Creating a task in a modal, including a JSON payload.
- A task card with `task`, `result`, dependencies, subtasks and the journal; an
  open card is written into the URL.
- Live updates over `EventSource`: the server announces changes, nothing polls.
- An expired lease, waiting dependencies, the attempt number and a non-zero
  priority are all visible in the row; the card carries the whole journal.
- A **Connect MCP** button with the address, the command and a config snippet,
  each copied in one click.

## Configuration

Every variable is listed with an explanation in [`.env.example`](.env.example) —
copy it to `.env` and edit what you need. An exported variable always wins over
the file, so `docker run -e` and `export` override `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `NOTED_DB` | `~/.noted/tasks.db`, `/data/tasks.db` in the image | the database file |
| `NOTED_HOST` / `NOTED_PORT` | `127.0.0.1` / `8787`, `0.0.0.0` in the image | where the core listens |
| `NOTED_UI_DIR` | `dashboard/dist` | the built dashboard |
| `NOTED_API` | `http://127.0.0.1:8787` | where the stdio adapter looks for the core |
| `NOTED_TOKEN` | empty | when set, `/api` and `/mcp` require `X-Noted-Token` |
| `NOTED_KEY` | empty | the agent key for the stdio adapter |
| `NOTED_LEASE_S` | `300` | lease length when the agent asks for none |
| `NOTED_SESSION_TTL_S` | `90` | how long a session survives without renewal |
| `NOTED_REAP_INTERVAL_S` | `15` | how often expired leases are collected |
| `NOTED_RETRY_BASE_S` / `NOTED_RETRY_CAP_S` | `5` / `300` | first retry pause and its ceiling |
| `NOTED_CREATE_LIMIT` / `NOTED_CREATE_WINDOW_S` | `300` / `60` | per-author creation limit; `0` disables |
| `NOTED_JOURNAL_KEEP_DAYS` | `30` | journal retention for closed tasks; `0` keeps everything |

## Operating it

**A single process is a requirement, not a preference.** There is exactly one
writer; several uvicorn workers would mean several writers and bring back the
cross-process race this service exists to remove. `workers=1` is baked into the
code and needs no overriding.

**Network.** Inside the container the process listens on `0.0.0.0`, and only
`127.0.0.1:8787` is published. If the core really has to be reachable from
elsewhere, hand out per-agent keys rather than a shared token.

**Access.** Agents arrive with a key in a header; the dashboard has its own
door — with `NOTED_TOKEN` set the browser asks for it once and keeps a pass in a
cookie. There are no roles inside the dashboard: whoever signs in is an admin.

**Data.** The database is one SQLite file in WAL mode, living in a volume. Back
it up by copying the directory with the container stopped, or with
`sqlite3 tasks.db ".backup out.db"` while it runs. Columns added in newer
versions are filled in when the database is opened: an older file neither breaks
nor needs a manual migration.

**A guard rail.** The queue is open, so creation is capped per author
(`NOTED_CREATE_LIMIT` per `NOTED_CREATE_WINDOW_S`). It catches a looping agent
rather than dividing the queue fairly — there are no quotas here.

**Watching it.** `HEALTHCHECK` polls `/healthz`, so `docker ps` shows not only
"running" but "answering". Abandoned tasks are collected by the service itself
once a lease expires; the `stale_seconds` filter and the journal remain for
understanding what happened.

**Where it stops.** The core is one process: if it falls, the swarm stops. There
is no replication and no failover — that is the price of having no broker.

## Development

The core:

```bash
python3 -m venv venv && venv/bin/pip install -e ".[dev]"
venv/bin/noted-api            # the core locally, without a container
venv/bin/python -m pytest
```

The dashboard (`dashboard/`) — React and TypeScript on astralyx-ui, built by Vite:

```bash
cd dashboard
npm install
npm run dev                   # 5173, with /api and /events proxied to the core
npm run build                 # builds dist/, which the core serves
```

Kit components live in `dashboard/src/components/ui` — they are copied into the
repository rather than pulled in as a dependency. To add one:
`npx astralyx-ui add <name>`.

### How the code is laid out

The core is split by layer and by domain: `routes/` is HTTP only, `services/`
holds all the logic together with the SQL, `models/` the schemas and the database
connection, `utils/` the cross-cutting parts. Services never import FastAPI, so
their tests start no application while the routes, MCP and the adapter all call
the same code.

```
api/          settings.py · models/ · routes/ (JSON, SSE, MCP) · services/ · utils/
mcp_adapter/  the stdio transport for clients without HTTP, no database access
dashboard/    React on astralyx-ui, built into dist/
deploy/       Dockerfile (two stages) and docker-compose.yml
tests/        services · routes · mcp
```

Requirements: Python ≥ 3.11, Node ≥ 20 (only to build the dashboard).

## Documentation

- [RECIPES.md](RECIPES.md) — acceptance through a verifier task, a branch or
  worktree in the payload, a supervisor for agents, integrations on events.
- [SPEC.md](SPEC.md) — the contract: model, statuses and transitions, outcomes,
  dispatch order, identity, the event format.
- [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md) — how it is built and
  why, an acceptance checklist and a table of risks.
