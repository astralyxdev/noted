# Technical documentation

How the task manager from `SPEC.md` is built: the layout, the decisions and the
reasons behind them, an acceptance checklist. Sections follow dependency order —
each one rests on the previous.

## Measured

One run, so read it as an order of magnitude rather than a specification.
The composed stack on a laptop: the core in its container as a single uvicorn
process, PostgreSQL 17 in another, both on Docker Desktop, everything over the
published loopback port. Agents are concurrent HTTP clients carrying their own
keys. The per-author creation limit was set to 0 for the throughput sweep —
it is a guard rail against a looping agent, not a thing worth measuring.

| operation | 1 agent | 4 | 16 | 64 |
|---|---|---|---|---|
| `create` | 339/s · p50 3 ms | 655/s · 5 ms | 649/s · 20 ms | 514/s · 71 ms |
| `claim` | 278/s · p50 3 ms | 855/s · 4 ms | 610/s · 22 ms | 483/s · 76 ms |
| create→claim→done | 98/s · p50 10 ms | 203/s · 19 ms | 197/s · 74 ms | 174/s · 269 ms |

Throughput flattens around four concurrent agents and then holds: past that
point the work is serialised by one Python process, and more agents buy
latency rather than throughput. p99 at 64 agents is 0.5 s for a single
operation and 0.7 s for a whole cycle. No errors at any width.

What matters more than the rate:

| under 64 agents claiming at once | result |
|---|---|
| 640 tasks claimed | 640 distinct — no task handed out twice |
| dispatch order | priority, then addressed before pool, then oldest — exactly as promised |
| a task with an unmet dependency | never handed out; claimable the instant its predecessor closed |

Recovery, with a 3-second lease and the collector on its default 15-second
round: an abandoned task was back in the queue **11 s** after the agent
stopped answering, with its holder cleared. Retries followed the documented
backoff — 5 s, then 10 s — and the third failure left the task `failed` with
`dead_letter` in its journal rather than retrying forever.

And the part no synthetic load shows: four Claude agents built a small static
site through the MCP endpoint, six tasks with a dependency chain, whoever was
free taking whatever was ready. The interesting number there is the hand-off.
From a predecessor being marked `done` to a waiting agent holding the task it
unblocked: **2–3 ms**, four times out of four. That is the long poll being
woken rather than timing out, and it is the difference between a dependency
graph that flows and one that advances once per poll interval.


## 0. Stack and layout

The core: Python 3.13, FastAPI with uvicorn, the `mcp` SDK, `httpx`, and one of
two storage engines — SQLite from the standard library, or PostgreSQL through
`psycopg` when `NOTED_DB_URL` is set. The dashboard: React and TypeScript on
[astralyx-ui](https://ui.astralyx.dev), Vite, Tailwind v4. Node is needed only at
build time — one Python process remains in the runtime.

The code is service-based: a layer decides *how*, a domain decides *what*. One
domain gets a file of the same name in every layer.

```
noted/
├── main.py                      # builds the FastAPI app: routers, MCP, static, uvicorn
├── .env.example                 # every variable, explained; copied to .env
├── requirements.txt
├── pyproject.toml               # the noted-api entry point
├── .dockerignore · .gitignore
├── README.md · SPEC.md · RECIPES.md · TECHNICAL_DOCUMENTATION.md
├── deploy/
│   ├── Dockerfile               # two stages: Node builds the front, Python serves it
│   └── docker-compose.yml       # core + PostgreSQL, loopback-only port, volumes
├── api/
│   ├── settings.py              # the only place the environment is read
│   ├── models/
│   │   ├── database.py          # picks the store; reading/transaction/locks
│   │   ├── schema.py            # the tables, written once, rendered per dialect
│   │   ├── sqlite_store.py      # one connection, one process lock, WAL
│   │   ├── postgres_store.py    # a pool, row locks, placeholder translation
│   │   ├── task.py              # pydantic: the task, statuses, request bodies
│   │   └── envelope.py          # the response envelope, Outcome enum, HTTP mapping
│   ├── routes/
│   │   ├── __init__.py          # router assembly
│   │   ├── tasks.py             # the JSON API
│   │   ├── live.py              # SSE at /events
│   │   └── mcp.py               # MCP over HTTP, mounted at /mcp
│   ├── services/
│   │   └── tasks.py             # task domain logic and all the SQL
│   └── utils/
│       ├── __init__.py          # run_service (a service call in a thread) and respond
│       ├── events.py            # two Conditions: work for agents, changes for dashboards
│       └── tool_docs.py         # tool descriptions, pinned against the code by a test
├── dashboard/                   # the frontend, built into dist/
│   ├── components.json          # astralyx-ui config: where components land
│   ├── vite.config.ts           # the @ alias, /api and /events proxied in dev
│   ├── index.html               # icon and manifest links
│   ├── public/                  # favicons and the manifest, copied into dist as they are
│   └── src/
│       ├── api.ts               # a typed client to /api
│       ├── hooks.ts             # filters in the URL, loading, SSE
│       ├── App.tsx              # the page assembly
│       ├── parts/               # FilterBar · TaskTable · NewTaskDialog · TaskDialog · ConnectDialog · LoginScreen
│       ├── components/ui/       # copies of astralyx-ui components (in the repo, not a dependency)
│       └── lib/                 # format.ts plus the kit's helpers
└── tests/
    ├── conftest.py              # a fresh database per test, a live uvicorn for streaming checks
    ├── services/                # test_tasks · test_lease · test_retry · test_deps · test_journal · test_cas · test_rate_limit · test_concurrency · test_journal_cursor
    └── routes/                  # test_tasks · test_dashboard · test_mcp_http · test_events_contract · test_review_findings
```

**Layer rules** — the reason the layout exists at all:

- `routes/` is delivery only: parse the request, call a service, wrap the answer.
  No SQL, no logic.
- `services/` holds the domain logic, SQL included. They never import FastAPI:
  they know nothing of `Request` or `HTTPException`, they return data and raise
  domain errors. That is why their tests start no application, while the JSON
  JSON routes and the MCP tools both call the same code.
- `models/` holds pydantic schemas and `database.py` with the connection and the
  schema. No logic.
- `utils/` is cross-cutting, tied to no domain.
- `settings.py` is the only place the environment is read. It sits at the root of
  the package deliberately: every layer may depend on it, and it depends on none.
  `.env` is loaded once at import and never overrides an already exported variable.
- A new domain means a new file of the same name in `routes/` and `services/`; a
  file that appears in only one layer means the domain was drawn wrong.

## 1. The database and the task domain

**Schema** (`models/database.py`):

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task          TEXT    NOT NULL,              -- JSON
    status        TEXT    NOT NULL DEFAULT 'pending',
    project       TEXT,                          -- which project, NULL means none
    assignee_id   TEXT,                          -- the current holder
    created_by    TEXT,
    parent_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    key           TEXT    UNIQUE,
    result        TEXT,                          -- JSON
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER,                       -- NULL means no retries
    retry_after   REAL,                          -- the pause after a failure
    lease_expires REAL,                          -- the lease term
    created_at    REAL    NOT NULL,              -- unix; ISO-8601 on the way out
    updated_at    REAL    NOT NULL
);

-- Dependencies. A cycle cannot be built by construction: links are set at
-- creation time, and nothing references a brand new task yet.
CREATE TABLE IF NOT EXISTS task_deps (
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id)
);


-- The transition journal: append-only.
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at REAL NOT NULL, event TEXT NOT NULL, actor TEXT,
    from_status TEXT, to_status TEXT, detail TEXT
);
```

**Migration.** The table is created, then `_migrate()` adds the columns listed in
`LATER_COLUMNS` (`PRAGMA table_info` → `ALTER TABLE ADD COLUMN`), and only then
are the indexes created — otherwise an index over a new column fails on a
database that predates it.

**The connection** is one per process, `check_same_thread=False`,
`isolation_level=None`, with `journal_mode=WAL`, `busy_timeout=10000` and
`foreign_keys=ON`. Every operation runs under a single `RLock`; the
`transaction()` context manager takes the lock and issues `BEGIN IMMEDIATE`.
That is the explicit serialisation: one writer, with the lock covering
concurrency inside the process.

**`services/tasks.py`** — `create`, `get`, `list_tasks`, `set_status`, `claim`,
`heartbeat`, `reap_expired`, `events`, `events_after`, `trim_journal`, `stats`,
`projects`, `assignees`. They return data rather than HTTP codes; malformed input
raises `TaskError` carrying a `code` (an `Outcome` value).

The load-bearing parts:

- Idempotency: `INSERT ... ON CONFLICT(key) DO NOTHING`; on `rowcount == 0` a
  `SELECT` by `key` returns the existing task with `created=False`. The UNIQUE
  index removes the race on insert.
- Compare-and-set: the status is read and written in one transaction; a mismatch
  with `if_status` returns the current task without writing.
- The atomic claim:
  ```sql
  SELECT id FROM tasks AS t
   WHERE t.status = 'pending'
     AND (t.assignee_id IS NULL OR t.assignee_id = :me)
     AND (t.project = :project)   -- only when a project was named
     AND (t.retry_after IS NULL OR t.retry_after <= :now)
     AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks p ON p.id = d.depends_on_id
                      WHERE d.task_id = t.id AND p.status <> 'done')
   ORDER BY t.priority DESC, (t.assignee_id IS NULL), t.id
   LIMIT 1;
  UPDATE tasks SET status='in_progress', assignee_id=:me, attempts=attempts+1,
                   lease_expires=:lease, updated_at=:now
   WHERE id = :id AND status = 'pending';
  ```
  Both statements sit in one transaction. `AND status='pending'` in the `UPDATE`
  is insurance should the serialisation ever be relaxed.
- `list_tasks` does not return `result`, and the limit is clamped to `[1, 500]`.
  The count of unmet dependencies (`waiting_on`) is a subquery in the same
  SELECT — otherwise listing would turn into N+1.
- `stats` counts inside the filter: otherwise, with a project selected, the
  dashboard chips lie.

**Leases and retries.** A claim sets `lease_expires = now + lease_s` and bumps
`attempts`. `heartbeat` extends the term, but only for your own task and only
while it is `in_progress`. `reap_expired()` runs every `NOTED_REAP_INTERVAL_S`
and returns overdue tasks to `pending`, dropping the holder; when attempts are
exhausted it sends them to `failed` with a `dead_letter` mark, or they would be
resurrected forever. The background loop lives in `main.py`: the collector calls
the service, not the other way round, so the service layer stays ignorant of any
scheduler.

`set_status(failed)` with attempts left does not write `failed` — it returns the
task to `pending` with `retry_after = now + backoff(attempts)`, with an
exponential backoff and a ceiling.

**No identity (by design).** Noted authenticates nobody: there is no `agents`
table, no keys, no sessions, and a project is a filter rather than a
permission. `assignee_id` and `created_by` are parameters, recorded as given.

The reasoning is in SPEC.md; the consequence for this layer is that the lease is
the only thing holding a task. `claim` takes one, `heartbeat` extends it, and
`reap_expired` hands back whatever ran out. `lease_s=0` opts out of expiry
entirely, which means the caller has taken on the job of putting the task back
if its agent dies — a supervisor can do that better than the core can, because
it is the thing that noticed the process exit.

**Compare-and-set by default.** `set_status` without `if_status` fills in
`in_progress`. An unconditional write is `force`, which is what a human at the
dashboard uses — a late agent reporting `done` on a task somebody cancelled is
refused, which is the whole point of the default.

**Dependencies.** Dispatch filters on a `NOT EXISTS (... p.status <> 'done')`
subquery, so "ready to be handed out" is never stored and cannot fall out of
sync. Blocking (`blocked`) and unblocking are separate transitions so both are
visible to a human and in the journal.

**The creation cap** is counted by an indexed query over `(created_by,
created_at)` in the same transaction — no in-memory state, and restarting the
core resets nothing. The check runs after the `key` lookup: an idempotent repeat
creates nothing and there is nothing to cut.

**The journal.** `_log()` writes in the same transaction as the change.
Heartbeats are deliberately not logged: they are frequent and carry no
transition. `trim_journal()` drops entries of closed tasks by age.

## 2. The response envelope (`models/envelope.py`)

`Outcome` is a string enum from the table in `SPEC.md`. `Envelope` is a pydantic
model with `ok`, `outcome`, `message` and a payload (`task` / `tasks` / `count` /
`events` / `stats` / `projects` / `assignees`). Next to it sits the project's one
and only `outcome → HTTP code` table.

The envelope lives in `models/` because both delivery layers use it — the JSON
routes and the MCP tools — so the shape of an answer cannot drift.

Serialisation uses `exclude_unset`: an explicit `task: null` survives, while
fields nobody filled in stay out of the response.

## 3. The core (`main.py`, `routes/`)

`create_app()` assembles the application: routers, the MCP transport, the
dashboard's static files, middleware and error handlers. It is a **factory**
rather than a module-level singleton — the MCP session manager may be run exactly
once in its lifetime, so every application needs its own.

`main.py` also owns the background lease collector: the task is created in the
lifespan and cancelled on shutdown. The collector does not die on a single error —
it logs and carries on.

`routes/tasks.py` holds the routes from the table in `SPEC.md`. Each one:
pydantic validation, a service call through `anyio.to_thread.run_sync` (the store is
synchronous and must not block the event loop), and wrapping into the envelope.

- Error handlers: `TaskError` becomes its own code and envelope;
  `RequestValidationError` becomes `validation_error` (FastAPI's own format is
  overridden, or an agent would get somebody else's shape of answer);
  `HTTPException` keeps its original status code; anything uncaught becomes
  `internal_error` with a log entry id and no traceback on the wire.
  `/healthz` and the dashboard need no header.
- The static files are mounted **last**, after the routers, so the root does not
  swallow `/api`, `/events` or `/mcp`. The directory comes from `NOTED_UI_DIR`.

## 4. Waiting on events (`utils/events.py`)

Two `asyncio.Condition`s per process: `available` (work appeared — wakes `claim`)
and `changed` (the state moved at all — wakes the dashboard's SSE streams).
Creating a task and returning one to `pending` pull both; claiming and other
status changes pull only `changed`.

Claiming with a wait: try to take a task → if empty and `timeout_s > 0`, wait on
the condition for the remaining time → try again once woken (being woken does not
mean the task went to this waiter) → answer `empty` at the deadline. `timeout_s`
is clamped to 300 seconds.

## 5. MCP: two transports

**Over HTTP (`routes/mcp.py`) — the main one.** An `MCPServer` from the SDK on
the streamable-http transport, mounted at `/mcp`. The tools call the same
services the JSON routes do. The core already listens on a port, so an agent
needs no separate process: the container is up, MCP is there.

The session manager lives in the application lifespan
(`session_manager.run()`), and it is exactly its run-once rule that makes the
application a factory.

There is no second transport. A stdio adapter existed and was removed: it was a
whole process, a second code path and a second set of failure modes, all to
reach a service that was already listening on a port. The one thing it did
uniquely — keeping a task alive through a long step without the model's
involvement — is now the supervisor's job, and `lease_s` is what it sets.

Tool descriptions live in `utils/tool_docs.py` — one source for the tools and
the JSON API, or the texts a model reads would drift apart from the behaviour.
A test pins them against the code, because that drift has happened twice.
Hard outcomes (`validation_error`,
`unauthorized`, `forbidden`, `rate_limited`, `internal_error`)
are raised as tool errors; `not_found`, `status_conflict` and `empty` come back as
ordinary results, because they are ordinary answers.

## 6. The dashboard (`dashboard/`)

React and TypeScript on astralyx-ui. Kit components are copied into the
repository (`npx astralyx-ui add …`) rather than pulled in as a dependency — the
code is ours and there is nothing to upgrade.

- `src/api.ts` — a typed client to `/api`. It unpacks the envelope: `ok=false`
  becomes an `ApiError` carrying the `outcome`, and an unreachable core becomes a
  readable message rather than "Failed to fetch".
- `src/hooks.ts` — filters are read from the query string and written back
  (`history.replaceState`), so a link to a filtered view works; `useDashboard`
  fetches the list and the overview in one go; `useLive` holds an `EventSource`
  and triggers a reload; `useNearBottom` pulls the next page through an
  `IntersectionObserver` once the bottom of the list approaches.
- Paging uses the `before_id` cursor rather than an offset: new tasks keep
  arriving while somebody scrolls, and an offset would start skipping rows. A
  live update re-reads the whole open window (up to 500 rows), so the list does
  not collapse to the first page after every event, and the tail past the ceiling
  stays as it loaded.
- `src/parts/` — `FilterBar`, `TaskTable`, `NewTaskDialog`, `TaskDialog`
  (`?task=<id>` in the URL), `ConnectDialog` (the MCP address, command and config
  with copy buttons). There is no sign-in: the core authenticates nobody.
- The mark in the header is `Wordmark` from the ui-kit (an inline SVG with
  blinking eyelids inheriting `currentColor`), a vertical separator and the
  product name. The blink keyframes are in `src/index.css`; under
  `prefers-reduced-motion` the eyelids simply stay closed. The icons are the same
  files the kit uses.
- The build puts static files into `dashboard/dist`, which the core serves from
  the root.

Frontend development: `npm run dev` runs Vite on 5173 and proxies `/api` and
`/events` to the core, so the backend needs no rebuild per change.

## 7. The event stream

`routes/live.py` serves the journal as SSE: a frame's `id` is the entry number
and the cursor at once. Reconnecting with `Last-Event-ID` or `?after=` replays
what was missed, which makes the stream usable for integrations rather than only
for lighting up the dashboard. A full batch is read without sleeping — that means
the stream is behind and catching up; an empty one waits on the condition with a
keepalive every 20 seconds.

## 8. Packaging and running

`deploy/Dockerfile` builds in two stages: Node installs the frontend's
dependencies and builds `dist`, then the Python image takes **only** the finished
directory — neither Node nor `node_modules` reaches the runtime. The database
lives in the `/data` volume so a rebuild does not wipe the tasks.

```
docker compose -f deploy/docker-compose.yml up -d
```

`HEALTHCHECK` polls `/healthz`, so `docker ps` shows not only "running" but
"answering".

`workers=1` is baked into `main()` and commented: several workers would mean
several writers and bring back the cross-process race.

An agent connects over HTTP with nothing to install:

```
claude mcp add --transport http noted http://127.0.0.1:8787/mcp/
```

That is the whole setup, and the only one: `http://host:port/mcp/`.

## 9. Tests

`pytest`, with the database in `tmp_path` through `NOTED_DB`. The test tree
mirrors the code tree.

- `services/test_tasks.py` — idempotency by `key`, CAS and conflict, addressed
  tasks against the pool, the strictness of the project filter, filters,
  `stale_seconds`, migrating an old database, the cursor, and a concurrent claim
  across threads. No application is started.
- `services/test_lease.py` — an expired lease returns the task to the pool, a
  heartbeat holds it, a foreign heartbeat and a foreign close get `not_owner`,
  `lease_s=0` is never collected.
- `services/test_retry.py` — a failure returns to the queue with a pause, the
  pause is respected on dispatch, exhausted attempts stay `failed`, and an
  expired lease on the last attempt goes to the dead letter.
- `services/test_deps.py` — a task waits for its predecessors, a failure blocks,
  a success unblocks, and dispatch order is priority, then addressing, then FIFO.
- `services/test_journal.py` — the whole path of a task is visible, and entries
  do not mix between tasks.
- `services/test_cas.py` — safe behaviour by default, and `force` as the explicit
  way to write unconditionally.
- `services/test_rate_limit.py` — a flood from one author is cut, other budgets
  are untouched, anonymous authors share a bucket, an idempotent repeat passes.
- `services/test_concurrency.py` — what has to hold when two callers arrive at
  once: every claimer gets its own task, one writer wins a compare-and-set.
- `routes/test_tasks.py` — a case per outcome, HTTP codes, the long poll, and
  that released work wakes a waiting agent.
- `services/test_journal_cursor.py` — the cursor never skips an entry that was
  still in flight, and never stalls on one that will never arrive.
- `routes/test_review_findings.py` — one test per defect found in review, so a
  fixed bug cannot come back quietly.
- `routes/test_events_contract.py` — events carry the whole transition, the
  cursor replays what was missed, and without a cursor the past is not replayed.
- `routes/test_dashboard.py` — `/api/stats` carries the chrome data and the built
  dashboard is served from the root.
- `routes/test_mcp_http.py` — a real SDK client: the tool list, a full cycle,
  errors and normal outcomes.

Streaming checks (SSE, MCP) run against a live uvicorn from the `live_server`
fixture: the in-memory ASGI transport does not deliver a stream incrementally.

## Acceptance checklist

- [ ] Two parallel claims on one task: exactly one `claimed`, the other `empty`.
- [ ] Two `set_status(..., if_status="in_progress")`: the second gets
      `status_conflict` and sees the current state.
- [ ] Creating twice under the same `key`: `outcome="exists"` and no second row.
- [ ] `claim_task(timeout_s=10)` wakes on a new task within milliseconds.
- [ ] An agent that crashed in `in_progress` is found by
      `get_tasks(stale_seconds=...)`.
- [ ] An agent dies holding a task: the lease expires and the task returns to the
      queue with no human involved.
- [ ] A failure with `max_attempts` retries with a growing pause and settles in
      `failed` once attempts run out.
- [ ] A task with an open dependency is handed to nobody and shows as waiting.
- [ ] A foreign agent can neither close nor extend a task it does not hold.
- [ ] Claiming inside a project takes neither another project's task nor an unscoped one.
- [ ] A database created before `project` existed opens and extends itself.
- [ ] `/mcp` answers an SDK client right after the container starts.
- [ ] The dashboard shows tasks, filters live in the URL, status changes from the
      row menu, and a long list pages by scrolling without collapsing on a live
      update.
- [ ] The image carries the built frontend but no Node; tasks survive recreating
      the container.
- [ ] The suite passes on **both** stores. `pytest -q` alone only exercises
      SQLite, where the process lock makes the row-locking clauses redundant —
      the one place the engines genuinely differ is then untested:
      `NOTED_TEST_DB_URL=postgresql://… pytest -q`
- [ ] `grep -r "sqlite3\|psycopg\|SELECT" api/routes` is empty: no SQL escaped
      the services.

## Risks and answers

| Risk | Answer |
|---|---|
| Several uvicorn workers bring back the cross-process race | `workers=1` in code, a comment, and a checklist item |
| A synchronous store blocks the event loop | every service call goes through `anyio.to_thread.run_sync` |
| Two engines, two schemas that drift | the tables are written once and rendered per dialect |
| PostgreSQL loses the guarantees the process lock gave | claims take `FOR UPDATE SKIP LOCKED`, read-then-write takes `FOR UPDATE` |
| A long poll ties up a connection and a thread | waiting on a `Condition` (the thread stays free), `timeout_s` clamped to 300 s |
| The MCP session manager is run twice | the application is built by a factory; each one gets its own server |
| A tool description promises what the code does not | a test pins the two together |
| Two writers decide from the same stale read | `row_lock()` before the decision, a status guard on the write |
| The journal cursor skips an entry that was still in flight | delivery stops at the last contiguous id |
| A health check that only proves Python is running | `/healthz` reaches the store |
| The collector dies on a transient error and nothing notices | every job of it is inside the guard |
| A task is resurrected forever | the attempt counts on claim; exhaustion means a dead letter |
| The lease collector dies quietly | the exception is logged and the loop continues |
| An agent closes another's work | `assignee_id` in `set_status`/`heartbeat`, outcome `not_owner` |
| A chain of dependants hides in `pending` | blocking walks the whole chain |
| The model cannot heartbeat during a long step | `lease_s` covers the step, or a supervisor puts the task back when the child exits |
| A forgotten `if_status` breaks invariants | CAS by default; an unconditional write is an explicit `force` |
| A looping agent floods the queue | a per-author creation cap; `rate_limited` arrives as a tool error |
| The journal grows without end | entries of closed tasks are trimmed by age |
| A pending task nobody can take | `waiting_on` in the list shows the open dependencies |
| A long list pulls thousands of rows at once | pages of 50 by cursor; the live window is capped at 500 |
| Large `task`/`result` bloat an agent's context | `result` only in `get_task`, list limit 500 |
| Open SSE connections pile up | keepalive every 20 s; a dropped connection closes the generator |
| The dashboard swallows /api, /events or /mcp | static files are mounted last, after the routers |
| The frontend knows the core's address | it uses relative paths; in development the Vite proxy supplies the address |
| The container listens on 0.0.0.0 and leaks into the network | only `127.0.0.1:8787` is published — and nothing beyond that is authenticated, so exposing the port is a decision that needs a proxy in front |
| Rebuilding the image wipes the tasks | the database is in the `/data` volume, not in an image layer |
| Settings scatter across the code | the environment is read only in `api/settings.py` |
