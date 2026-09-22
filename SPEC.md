## Task manager

A lightweight task manager that lets agents hold a persistent connection or
work iteratively.

### Architecture

Three thin layers:

```
agents ──────── MCP over HTTP (/mcp) ───────>┌──────────────────────────────┐
                                             │      FastAPI + SQLite        │
agents ─MCP(stdio)─> adapter ──HTTP─────────>│ API · MCP · SSE · dashboard  │
                                             │                              │
browser ──── static + /api + SSE ───────────>└──────────────────────────────┘
```

1. **The FastAPI core** owns the database. All the logic and all the SQL live here.
2. **MCP** comes in two transports. The main one lives inside the core at `/mcp`
   (streamable-http): the container is up, so the server is already there and no
   separate process is needed. For clients that cannot speak the HTTP transport
   there is a stdio adapter — a process per agent, stateless, with no database
   access, translating calls into HTTP requests to the core.
3. **The web dashboard** — React on [astralyx-ui](https://ui.astralyx.dev), built
   into static files and served by the same application from the root. It uses
   the same `/api` the agents do and updates over SSE. There is no separate
   runtime for the frontend: Node exists only at build time.

Inside the core the code is split by layer and by domain: `routes/` is delivery
only, `services/` holds the logic together with the SQL, `models/` the schemas
and the database connection, `utils/` the cross-cutting parts. A domain gets a
file of the same name in each layer. The layout and the rules are in
`TECHNICAL_DOCUMENTATION.md`.

What this buys in terms of races: if every agent ran its own process and they
all wrote to one database file, the race would be cross-process. There is
exactly one writer. That alone is not enough, though — requests inside the
process are still concurrent, so the serialisation is explicit: one SQLite
connection under a lock, `uvicorn --workers 1`. Several workers bring the
original problem back.

Waiting stops being polling as a bonus. Two `asyncio.Condition`s per process:
`available` wakes agents on `claim`, `changed` wakes open dashboards. They are
split deliberately — claiming a task changes what the dashboard shows, but
there is no reason to wake agents that are waiting for work.

### Storage

SQLite, one file, WAL mode. The default path is `~/.noted/tasks.db`, overridden
by `NOTED_DB`; in the container it is a volume mounted at `/data`. Columns added
after the first version of the schema are filled in when the database is opened:
an older file neither breaks nor needs a manual migration. WAL stays on for
outside readers (`sqlite3` in a neighbouring terminal), while the writer remains
single.

Network: the core listens on `127.0.0.1:8787` (`NOTED_API` on the adapter side).
An optional `NOTED_TOKEN` is checked by middleware on `/api` and `/mcp`; the
adapter and other clients send it in the `X-Noted-Token` header.

### Task model

| Field | Type | Purpose |
|---|---|---|
| `id` | int | primary key, autoincrement |
| `task` | JSON object | the payload, arbitrary |
| `status` | enum | see below |
| `project` | string, nullable | scope: an agent naming a project works only with its tasks |
| `assignee_id` | UUIDv4/String/Integer, nullable | `null` means the task is in the shared pool |
| `created_by` | UUIDv4/String/Integer, nullable | who posted it — so delegated work and its results can be found |
| `parent_id` | int, nullable | the subtask tree, with no scheduler attached |
| `key` | string, nullable, unique | idempotency key: a retry breeds no duplicates |
| `result` | JSON, nullable | the result, or the reason for failure |
| `priority` | int, default 0 | higher goes out sooner |
| `attempts` | int | how many times the task has been claimed |
| `max_attempts` | int, nullable | attempt ceiling; `null` means no retries |
| `retry_after` | timestamp, nullable | the task is not handed out before this moment |
| `lease_expires` | timestamp, nullable | lease term; once past, the task returns to the queue |
| `session_id` | string, nullable | which session holds the task |
| `depends_on` | list[int] | predecessors that must reach `done` |
| `waiting_on` | int, read-only | how many predecessors are still open |
| `created_at` | timestamp | |
| `updated_at` | timestamp | what `stale_seconds` searches by |

### Identity

Until a key is issued the core runs as an open queue: anyone may post and claim,
and `assignee_id` is a self-declared name. The first key turns identity on, and a
local install is not broken by an upgrade — strictness arrives by an explicit act.

**An agent is a principal, not a string.** A key is issued with `noted-keys add`
and shown once; only its hash is stored. The key arrives in the `X-Noted-Token`
header and the server derives `assignee_id` from it. Taking another's name is not
possible: for an agent the `assignee_id` parameter is ignored, and `created_by`
is filled in by the server too.

**The scope is enforced.** A key carries a list of projects. A scoped agent
neither sees nor claims another project's task, and posting into one answers
`forbidden`. Tasks outside every project stay open to all: a shared pool is
shared on purpose.

**A session is one instance of an agent.** The same key can run in two processes,
and by name they are indistinguishable. So a task is owned by a pair — agent plus
session — and a zombie on an old session gets `stale_session` even when the name
matches. That is fencing, and no separate claim token is needed for it.

**The transport renews the session, not the model.** An LLM agent can only call
tools between steps: while a ten-minute build runs it sends no heartbeat at all.
Proof of life therefore comes from a process rather than from reasoning:

- the stdio adapter lives exactly as long as its client and renews in the background;
- over HTTP the session is `Mcp-Session-Id`, renewed by any request on the connection.

Inside a session a task takes **no time-based lease**: it is held for as long as
the session lives. When the process dies the session expires after
`NOTED_SESSION_TTL_S` (90 seconds) and everything it held is released at once.
That is faster than a five-minute lease and asks nothing of the model.

What identity does not provide: roles and users inside the dashboard. There is
one door — whoever signs in is an admin.

### Compare-and-set by default

`set_status` without `if_status` does not write unconditionally: the server fills
in `in_progress`, the only state an executor may report from. A forgotten
`if_status` no longer breaks invariants in silence (`done` → `pending`,
`failed` → `done`).

An unconditional write is an explicit `force=true`. That is how a human acts from
the dashboard: cancelling a task in any state is their right, but it is a
separate intention rather than a default.

A move into a terminal status releases the session. Otherwise a human would
cancel a task and a returning agent would silently overwrite that with `done`.

### Transition semantics

| Event | What happens |
|---|---|
| `claim` | `pending` → `in_progress`, `attempts+1`, the task binds to the session |
| the executor closes it | `in_progress` → `done` / `failed` / `blocked`, the session is released |
| a failure with attempts left | `failed` turns into `pending` with a `retry_after` pause |
| a failure with attempts gone | it stays `failed` — that is the dead letter |
| the lease expired or the session died | `in_progress` → `pending` (or `failed` when attempts are gone) |
| a predecessor reached `done` | waiting tasks go `blocked` → `pending` if nothing else is open |
| a predecessor `failed` or was `cancelled` | waiting tasks go `pending` → `blocked` |
| a human in the dashboard | any transition, through `force=true` |

A task does not leave `blocked` by itself: either a predecessor's success
unblocks it, or a human or orchestrator moves it. That is deliberate — whether
to do work whose premise failed is a decision for whoever writes the
orchestration, not for the queue.

Cancelling a predecessor blocks its waiters exactly as a failure does:
`cancelled` means "this will not happen", and there is nothing left to wait for.

### Dispatch order

A contract, not an implementation detail:

```
priority ↓ → addressed before the pool → FIFO by id
```

Never handed out: tasks still inside a post-failure pause, tasks with open
dependencies, and tasks of other projects for a scoped key.

Priority comes first on purpose. Were addressing first, a low-priority task
addressed to an agent would overtake an urgent one from the pool, and urgency
would stop meaning anything.

**Starvation is possible and that is a deliberate choice.** A steady stream of
high-priority work can hold off the low-priority indefinitely; there is no
priority ageing in the core. Whoever needs fairness computes it outside and
adjusts `priority`.

### The creation guard rail

The queue is open: anyone may post, and the first free agent takes it. The other
side of that is a looping agent flooding the queue with a thousand tasks in
seconds, with nobody left to stop it.

So creation is capped: no more than `NOTED_CREATE_LIMIT` tasks per
`NOTED_CREATE_WINDOW_S` from one author (`created_by`). Crossing it answers
`rate_limited`, and an agent receives that as a tool error rather than a quiet
"no": otherwise the loop that got it there keeps spinning.

- What is counted is tasks actually created, not calls. An idempotent repeat
  under the same `key` creates nothing and is not capped.
- Authors are counted separately; everyone who did not name themselves shares one
  bucket, or an empty field would bypass the guard.
- The default is 300 tasks a minute: honest decomposition into a hundred subtasks
  passes, pathology does not. `NOTED_CREATE_LIMIT=0` switches the check off.

This is a guard rail, not a quota: it catches breakage rather than dividing a
resource between agents.

### Projects

A task may belong to a project — an arbitrary string, with `null` meaning
"outside every project". The scope is strict: an agent naming a project in
`claim_task` gets neither another project's task nor one with no project. Name
none and it takes from anywhere. This lets several projects share one database
without getting in each other's way, and needs neither separate instances nor a
project table: the list of projects is derived from the tasks themselves.

### Statuses

A fixed set, validated on the server — otherwise agents will write `done`,
`DONE`, `completed`, `finished`, and filtering dies.

`pending` · `in_progress` · `blocked` · `done` · `failed` · `cancelled`

Terminal: `done`, `failed`, `cancelled`. There is no state machine — any
transition is allowed, and `if_status` is what controls them.

### HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/tasks` | create a task |
| `GET` | `/api/tasks` | list with filters |
| `GET` | `/api/tasks/{id}` | one task in full, with its result |
| `PATCH` | `/api/tasks/{id}/status` | change status |
| `POST` | `/api/tasks/claim` | take a task atomically, with long poll and lease |
| `POST` | `/api/tasks/{id}/heartbeat` | extend the lease |
| `GET` | `/api/tasks/{id}/events` | the transition journal of a task |
| `GET` | `/api/stats` | per-status counters, plus project and assignee lists |
| `GET` | `/events` | the SSE stream of changes |
| `POST` | `/api/sessions/renew` | session renewal by the transport |
| `POST` | `/api/login` · `/api/logout` | the dashboard door (a browser sends no headers) |
| `POST`/`GET` | `/mcp/` | MCP over the streamable-http transport |
| `GET` | `/healthz` | liveness |

Bodies and semantics match the MCP tools below — the adapter renames nothing.

### Answers and errors

An agent always receives an explicit outcome rather than a bare result or a raw
exception. One envelope for every command:

```
{ ok[bool], outcome[string], message[string/null], ...payload }
```

`outcome` is a machine-readable code, and an agent decides what to do next by
it. The field is not called `status` so it cannot be confused with the task's own.

| `outcome` | `ok` | HTTP | When |
|---|---|---|---|
| `created` | true | 201 | the task was created |
| `exists` | true | 200 | the idempotency key matched; nothing was created |
| `ok` | true | 200 | an ordinary read |
| `updated` | true | 200 | the status changed |
| `claimed` | true | 200 | the task was taken by an executor |
| `empty` | true | 200 | nothing to take, the long poll ran out — not an error |
| `not_found` | false | 404 | no task with that id |
| `status_conflict` | false | 409 | `if_status` did not match, or the task is in another state |
| `not_owner` | false | 409 | the task is in progress with another executor |
| `stale_session` | false | 409 | another instance of the same agent holds it |
| `forbidden` | false | 403 | the project is outside the key's scope |
| `parent_not_found` | false | 404 | the given `parent_id` does not exist |
| `unauthorized` | false | 401 | `NOTED_TOKEN` is set and `X-Noted-Token` did not match |
| `validation_error` | false | 422 | malformed input: wrong type, unknown status, empty `assignee_id` |
| `rate_limited` | false | 429 | the author posts faster than the limit |
| `api_unavailable` | false | — | the core is not answering; returned by the adapter, with the API address |
| `internal_error` | false | 500 | everything else, with a log entry id |

Expected negative outcomes (`not_found`, `status_conflict`, `empty`) are ordinary
answers, not exceptions: an agent must handle them without crashing. Malformed
input, a refused key and an unreachable core are additionally marked `isError` in
MCP so an agent does not mistake them for a result.

### MCP tools

The tools are identical across both transports, and their descriptions come from
one place so the texts a model reads cannot drift apart.

```
set_task(task[JSON object], project[string/null], assignee_id[UUIDv4/String/Integer/null],
         parent_id[int/null], key[string/null], created_by[UUIDv4/String/Integer/null],
         priority[int=0], max_attempts[int/null], depends_on[list[int]/null])
    -> {ok, outcome: created|exists|validation_error|parent_not_found|forbidden, task}

get_tasks(assignee_id[.../null], status[string/list/null], project[string/null], unscoped[bool],
          unassigned[bool], parent_id[int/null], stale_seconds[number/null],
          before_id[int/null], limit[int=50])
    -> {ok, outcome: ok|validation_error, tasks[list], count[int]}

get_task(task_id, with_events[bool=false])
    -> {ok, outcome: ok|not_found, task, events[list]}

set_status(task_id, status[enum], result[JSON/null], if_status[enum/null],
           assignee_id[.../null], force[bool=false])
    -> {ok, outcome: updated|not_found|status_conflict|not_owner|stale_session|validation_error, task}

claim_task(assignee_id, project[string/null], timeout_s[number=0], lease_s[number/null])
    # with a key: assignee_id comes from the key, and a session replaces the lease
    -> {ok, outcome: claimed|empty|validation_error|forbidden, task}

heartbeat(task_id, assignee_id, lease_s[number/null])
    -> {ok, outcome: updated|not_found|status_conflict|not_owner|stale_session, task}
```

`set_task` — when `key` matches an existing task nothing is created:
`outcome="exists"` and the old task comes back in `task`. An agent tells "created"
from "already there" by the code and does not do the work twice.

`get_tasks` — sorted by `id` descending (newest first). `result` is not included;
use `get_task` for it, or a page of 50 tasks would burn an agent's context.
`status` takes a string or a list (`["pending","in_progress"]` is everything open).
`project` narrows to one project, `unscoped=true` returns only unscoped tasks, and
`unassigned=true` only the shared pool. `stale_seconds` finds tasks not updated for
N seconds — that is how in_progress work left by a dead agent is found. `before_id`
is the paging cursor: the next page is everything older than that id. There is
deliberately no offset — new tasks keep arriving while you page, and an offset
would start skipping rows.

`set_status` — `result` is written for success and failure alike (on `failed`, put
the reason there). Compare-and-set is the default; see above.

`claim_task` — atomically takes one `pending` task and moves it to `in_progress`.
Dispatch order is the contract above. `project` narrows the search strictly.
`timeout_s=0` is a non-blocking pop; `timeout_s>0` is a long poll waiting up to N
seconds and answering `outcome="empty"` when it runs out. That is the promised
"persistent connection": no websockets, no broker, and no busy polling.

If the core is unreachable the stdio adapter answers `outcome="api_unavailable"`
with the API address in `message`, rather than a timeout or a traceback.

### The event stream

`GET /events` is a public contract, not dashboard decoration. Every change
arrives as a journal entry:

```
id: 128
event: task
data: {"id":128,"task_id":7,"at":"2026-09-22T18:31:00Z","event":"claimed",
       "actor":"agent-1","from_status":"pending","to_status":"in_progress","detail":{…}}
```

`id` is the journal entry number and the cursor at once. After a disconnect a
client sends `Last-Event-ID` (a browser `EventSource` does it by itself) or
`?after=<number>` and receives **everything it missed**, not only what is new.
Without a cursor the stream starts from the present moment.

Event kinds: `created`, `claimed`, `status`, `retry`, `reaped`, `dead_letter`,
`blocked`, `unblocked`. External integrations are built on them — acceptance,
launching agents, wiring a chat — which is why none of that belongs in the core
(see `RECIPES.md`).

### Journal retention

The journal is append-only, so it grows. Entries of closed tasks (`done`,
`failed`, `cancelled`) older than `NOTED_JOURNAL_KEEP_DAYS` (30 days) are removed
by a background pass. The journal of a live task is never touched, however old it
is: that is what an ongoing incident is reconstructed from.
`NOTED_JOURNAL_KEEP_DAYS=0` keeps everything.

### The web dashboard

React and TypeScript on [astralyx-ui](https://ui.astralyx.dev), built by Vite and
served by the core from the root. It runs on the same `/api` the agents use:
there is no separate "frontend API", and anything the dashboard can do an agent
can do too.

- **The task list** — a table with `#`, status, project, task, assignee, updated.
  The status is a coloured badge; an expired lease, waiting dependencies, the
  attempt number and a non-zero priority show up in the row itself. Row actions
  are a menu: open, or move to any status.
- **Filters** — status chips with counters (multi-select) plus project, assignee
  and "not updated for N". Counters are computed inside the selected project, not
  over the whole database. The state lives in the query string, so a filtered
  view can be shared as a link.
- **Paging** — pages of 50 as you scroll; a live update re-reads the window that
  is already open (up to 500 rows), and the tail beyond it stays as it loaded:
  those are old tasks and they barely change.
- **A summary** — total, in progress, queued, stuck.
- **New task** — a header button opens a modal: title, project, assignee, and an
  optional JSON payload merged with the title. The project is taken from the
  active filter. Malformed JSON is not sent and is explained in place.
- **A task card** — a modal with `task`, `result`, attempts, remaining lease,
  retry pause, dependencies, subtasks, the transition journal and a status
  change. An open card is written into the URL (`?task=12`), so a link to a task
  can be shared too.
- **Live updates** — `EventSource("/events")`. The exchange runs one way, so SSE
  rather than a websocket: reconnection is built into the browser, the transport
  is plain HTTP, and there are no dependencies. On an event the list and the
  counters are re-read while open forms are left alone. The indicator stays quiet
  while the connection holds and speaks only when it breaks. Without
  `EventSource` it falls back to polling every 10 seconds.
- **Connect MCP** — a header button showing the server address, the command for a
  client and a ready config snippet, each copied in one click.
- **Sign in** — with `NOTED_TOKEN` set the dashboard asks for it and keeps a pass
  in a cookie: a browser cannot send the header the agents use. There are no
  roles — whoever signs in is an admin.
- **The mark** — the Astralyx logo from the ui-kit, a vertical separator, and the
  product name to the right.

### What is deliberately left out

Worker pools and a scheduler with windows, roles and users, replication.

About the boundary: leases, retries, dependencies and priority are already here —
without them a swarm does not survive. The next step past them is long-running
workflows with compensation and distributed execution, and that is where one
honestly reaches for Temporal instead of writing more of this.

What is missing and why:

- **No acceptance in the core.** `done` means "the agent said done". Review is
  assembled from a verifier task through `depends_on` — the recipe is in
  `RECIPES.md`.
- **No isolation of working copies.** The queue separates agents by task, not by
  file. A branch or a worktree goes into the payload, put there by the orchestrator.
- **It launches no executors.** Keeping agents alive is a supervisor's job, outside.
- **No contract on the shape of a payload.** `task` is arbitrary JSON; agreeing on
  its fields stays with whoever writes the orchestration.
- **No roles or users.** The dashboard has one door.
- **No replication or HA.** There is one writer; if the core falls, the swarm
  stops. That is the price of having no broker.
- **No capability routing.** A task is addressed by a string, not to "an agent
  that can do X".
- **No quotas and no priority ageing.** The creation limit is a guard rail against
  a loop, not a fair division of the queue.

Re-queueing stuck tasks needs no orchestrator: an expired lease or a dead session
is collected by the service itself. `get_tasks(stale_seconds=...)` and the
journal remain for understanding what happened.
