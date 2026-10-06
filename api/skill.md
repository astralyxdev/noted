---
name: noted
description: Work through the Noted task queue over MCP — plan work as tasks, decompose it, claim tasks one at a time, report results. Use whenever a project's work is coordinated through Noted, when asked to take, post, plan, split or close tasks, or when several agents share one queue.
---

# Noted

Noted is a task queue for agents. Each task goes to exactly one holder; a holder
that dies loses the task back to the queue when its lease runs out; every change
is journaled. You talk to it through MCP tools.

Server: `{{BASE_URL}}` · MCP: `{{BASE_URL}}/mcp/` · dashboard: `{{BASE_URL}}/`

Contents: 1 Connect · 2 Rules · 3 Executing · 4 The task object ·
5 Relations · 6 Decomposition · 7 Posting a plan · 8 Looking around

## 1. Make sure the MCP is connected

Check whether the tools `claim_task`, `set_task`, `set_status` are available
(often prefixed, e.g. `mcp__noted__claim_task`). If they are not:

```bash
claude mcp list | grep -q noted || claude mcp add --transport http noted {{BASE_URL}}/mcp/
```

Other clients take this config:

```json
{"mcpServers": {"noted": {"type": "http", "url": "{{BASE_URL}}/mcp/"}}}
```

The tools appear after the client restarts or reloads its MCP servers. There is
no authentication. If `{{BASE_URL}}/healthz` does not answer, the server is down;
say so instead of working around it.

To keep this skill locally:

```bash
mkdir -p ~/.claude/skills/noted && curl -fsS {{BASE_URL}}/skill.md -o ~/.claude/skills/noted/SKILL.md
```

## 2. Rules that matter

- **Branch on `outcome`, not on `ok`.** Every answer is
  `{"ok", "outcome", "message", ...}`. `empty`, `not_found`, `status_conflict`,
  `not_owner` and `exists` are normal answers that tell you what to do next.
- **`assignee_id` is your name, and it must be unique.** Use what you were given
  (e.g. `agent-3`). Two agents with one name are one holder to the queue.
- **Always pass `project`.** The project filter is strict, so you never pick up
  another project's work.
- **The lease is the only thing holding your task.** The default is 300 s. Ask
  for a `lease_s` that covers your longest step, or call `heartbeat` between
  steps. Never use `lease_s: 0`: then nothing returns the task if you die.
- **Never use `force: true`.** It is for a human at the dashboard. Losing a
  compare-and-set means somebody else changed the task, so read it again.
- **One task at a time.** Close the one you hold before claiming the next.
- **Stay inside the task.** Do what `do` says, to `done_when`. Anything else you
  notice becomes a new task (§5, follow-up), not a silent extra change.

## 3. Executing: the loop

```
claim_task(assignee_id="agent-3", project="lino", timeout_s=60, lease_s=1800)
```

| outcome | do |
|---|---|
| `claimed` | the task is yours: `task.id`, body in `task.task` |
| `empty` | nothing came within `timeout_s`; claim again, or stop if told to |

`timeout_s` is a long poll (max 300): the call waits and returns as soon as work
appears. Do not add your own sleep loop.

Then:

1. **Read the body** (§4). If the task has dependencies, call `get_task(task_id)`
   and read each predecessor's **result**: the files, signatures and decisions
   you build on. Do not re-derive what a predecessor already decided.
2. **Read the `spec`** it points to. The body is the summary; the spec is the
   source of truth.
3. **Do the work** exactly to `do`, producing the `api` it names, touching the
   `files` it names. On a long task call `heartbeat(task_id, assignee_id)`
   between steps.
4. **Verify every `done_when` line** by running it, not by assuming it.
5. **Report**:

```
set_status(task_id=9194, status="done", assignee_id="agent-3", result={...})
```

| outcome | meaning |
|---|---|
| `updated` | recorded |
| `status_conflict` | the task is no longer `in_progress` (cancelled, or your lease expired and it was re-queued). Stop working on it; `get_task` to see why |
| `not_owner` | someone else holds it now. Stop |
| `not_found` | wrong id |

Close with `failed` only when the work **could not be done**, with the reason in
`result` (`{"error": "...", "tried": [...]}`). A task with `max_attempts` left
goes back to the queue after a pause. If `done_when` holds but something is
missing, it is still `done`; list what is missing in `gaps`.

### The result object

`result` is how the next agent learns what you did. It is not a chat message.
Make it specific enough that a dependent task can start without reading your
diff:

```json
{
  "files": ["internal/registry/id.go", "internal/registry/id_test.go"],
  "api": ["const registry.IDLen = 6",
          "func registry.IDFor(canonicalRoot string) string",
          "func registry.IDForDir(dir string) (string, error) // canonicalises via paths.Canonical",
          "func registry.ValidID(s string) bool"],
  "decisions": "id = first 8 bytes of sha256(canonical path) as uint64, low 6 base36 digits (lowercase), zero-padded.",
  "gaps": "--name alias resolution not included (belongs to registry/resolution tasks)."
}
```

| field | contents |
|---|---|
| `files` | every file created or changed, with a short note where useful |
| `api` | the exact signatures, endpoints, CLI commands or schemas others will call |
| `decisions` | choices a later task must respect, and the numbers behind them |
| `gaps` | what is not done or not verified. Never leave this out to look finished; write `"none"` when there are none |
| `verified` | optional: the `done_when` commands you ran and what they printed |
| `decision`, `numbers` | spikes: the choice made and the measurements behind it |
| `split_into`, `join` | if you decomposed the task (§6) |
| `verdict`, `why` | verify tasks: `"accepted"` or `"rejected"`, and the reason |

## 4. The task object

`task` is free JSON, but every task in a project should share one shape, so any
agent can pick up any task cold. This shape carried a 130-task build to the end.

### A build task

```json
{
  "title": "L01 Process id derivation",
  "milestone": "3. Live process",
  "spec": "scope.md: Model > Process id",
  "do": "6-char lowercase base36 id from hash of canonical root path. Stable across runs. Helper to validate id syntax.",
  "api": ["func registry.IDFor(canonicalRoot string) string", "func registry.ValidID(s string) bool"],
  "files": ["internal/registry/id.go", "internal/registry/id_test.go"],
  "done_when": [
    "tests: same root => same id, different roots differ, symlinked root path => same id",
    "go test ./internal/registry/ passes"
  ]
}
```

### A fix or follow-up task

```json
{
  "title": "Fix: cut cost of direct-mode pre-query stat pass (RefreshAll)",
  "found": "RefreshAll costs ~8 ms on 5k files / 1M lines and adds ~17 ms per call on 11k files, so a direct search goes from 9 to 26 ms.",
  "repro": "LINO_BIG=1 go test -run TestRefreshAllLargeCorpus ./internal/index/",
  "found_in": 9309,
  "spec": "scope.md: Targets (cold call overhead < 15 ms)",
  "do": "Covering index files(path,size,mtime) via schema v2 + migration; measure on Linux too.",
  "done_when": [
    "TestRefreshAllLargeCorpus reports best < 5 ms on 5k files",
    "e2e TestIndexExternalNewContent still passes"
  ]
}
```

### Field reference

Required in every task:

| field | type | what goes in it |
|---|---|---|
| `title` | string | `<code> <name>`. The code is a letter for the area plus a number for the order (`F01`, `I02`, `L01`), or `Fix:` / `Verify:` / `Spike:`. Readable on the board, and it is the tail of the `key` |
| `do` | string | What to build, concretely: names, paths, flags, limits, edge cases. Enough for an agent with no other context to start. Imperative, no history |
| `done_when` | string[] | Checkable acceptance. Each line is a command with an expected result, or a test with named cases. The executor runs them, and a verifier checks against them. No line like "works well" |

Required when it applies:

| field | type | when |
|---|---|---|
| `spec` | string | there is a design document: `file: Section > Subsection`. Point to it rather than copying it; the spec stays the source of truth |
| `milestone` | string | the plan has stages: `"<n>. <name>"`, the same string on every task of that stage |
| `api` | string[] | the task produces an interface others build on: exact signatures, endpoints with method and body, CLI usage. This fixes the contract before anyone writes code, so dependent tasks can be written now |
| `files` | string[] | the touch set is known: files to create or change. Two tasks with overlapping `files` must be ordered with `depends_on` |
| `found` | string | fix / follow-up: what was observed, with the numbers |
| `repro` | string | fix: the shortest command that shows the problem |

Optional:

| field | type | use |
|---|---|---|
| `needs` | string[] | what this task uses from its predecessors ("registry.IDFor from L01"), so the executor knows which results to read |
| `out_of_scope` | string[] | things that look related but belong to other tasks, so the executor does not drift into them |
| `note` | string | context that is not an instruction (why, history, a warning) |
| `severity` | `"info"`/`"low"`/`"medium"`/`"high"`/`"critical"` | findings and fixes |
| `found_in` | int | fix / follow-up: the task in which it was found |
| `verifies` | int | verify task: the task being checked |
| `supersedes` | int[] | a re-planned task: the old tasks it replaces |
| `branch`, `worktree`, `base` | string | the orchestrator isolates working copies per task |

Rules for writing the body:

- **Self-contained.** No "as discussed" and no "see above". The executor has
  this body, the spec and the predecessors' results, nothing else.
- **Concrete over descriptive.** `"lino read <file> [--lines A:B] [--anchors];
  500-line limit => outcome truncated"` beats "implement a read command".
- **Numbers, not adjectives.** `"p95 < 10 ms on 1M lines"`, not "fast".
- **One task, one result.** If `do` contains "and also", it is probably two
  tasks.
- **Pointers to other tasks are informational.** `found_in`, `verifies` and
  `supersedes` are for people and agents to read; the server enforces only the
  relations in §5.

## 5. Relations

There are two kinds. **Server relations** are parameters of `set_task`, and the
server acts on them. **Payload relations** are fields in the task body, and the
server does not act on them; agents and people read them.

### Server relations

**`depends_on: [ids]`: ordering, the only relation that gates.**
- A task is not handed out until every predecessor is `done`. Meanwhile it is
  `pending` with `waiting_on > 0` (the number of predecessors still open).
- If a predecessor becomes `failed` or `cancelled`, its dependants (and theirs,
  down the chain) become `blocked`. If a predecessor was already failed or
  cancelled when the task was posted, it is born `blocked`.
- A `blocked` task returns to `pending` by itself when all of its predecessors
  are `done`, for example after the failed one is fixed and re-run. Nothing
  else unblocks it.
- Add an edge only when the task needs the other's **output** (a file, an API,
  a decision) or touches the same files. Every extra edge makes the plan run
  more serially.
- Dependencies are fixed at creation. You cannot add or remove an edge later;
  re-plan instead (supersede, below).

**`parent_id: id`: the tree, for grouping only.**
- Groups subtasks under an umbrella (a milestone, a feature, a task that was
  split). `get_tasks(parent_id=N)` lists the children.
- It does **not** gate anything: a child can run before or after its parent,
  and closing the parent does nothing to the children. If the order matters,
  use `depends_on`.

**`key: string`: identity and idempotency.**
- Globally unique across all projects. Posting the same `key` again returns the
  existing task with `outcome: "exists"`, so re-running a plan breeds no
  duplicates.
- Use a path: `<project>/<plan>/<code>`, e.g. `lino/v2/F04`; fixes
  `lino/v2/fix-<slug>`; children of a split `lino/v2/F04/1`, `lino/v2/F04/2`.
  The path doubles as a readable hierarchy.

**`project: string`: the scope.** Claiming with a project is strict: you get
only that project's tasks. Everything in one product shares one project.

**`assignee_id` at creation: addressing.** Posting with `assignee_id` reserves
the task for one agent; it is handed only to them. Leave it `null` for the
shared pool. Use it only when exactly that agent must do the work (it holds the
context, or it is the verifier pinned to be different from the builder).

**`priority: int`: order within what is ready.** Higher goes first, then tasks
addressed to you, then oldest. It never overrides `depends_on`.

### Patterns built from them

| pattern | how |
|---|---|
| **Sequence** A → B | B `depends_on=[A]` |
| **Fan-out** A → B, C, D | each `depends_on=[A]`; they run in parallel |
| **Fan-in / join** B, C, D → E | E `depends_on=[B, C, D]` |
| **Milestone gate** | one `"<n>.x Milestone acceptance"` task that depends on every task of the milestone, with an end-to-end `done_when`. The next milestone's first tasks depend on the gate |
| **Spike → decision** | a high-priority spike whose result states the decision (`decision`, `numbers`); every task that rests on the decision depends on it |
| **Contract first** | an interface task that only fixes `api` (types, signatures, schemas, stubs); implementations and their callers both depend on it and then run in parallel |
| **Verify** | `Verify: <title>` with `depends_on=[work]` and `verifies: work`, done by a *different* agent. It closes `done` with `verdict` and `why`; on `"rejected"` it posts a fix task. Use `failed` only if the check itself could not be run |
| **Follow-up / fix** | a new task with `found`, `repro`, `found_in: <id>` and `key: <project>/<plan>/fix-<slug>`. Make it depend on the task it fixes only if that task is still open |
| **Supersede** | post the new tasks (new plan segment, `v2` → `v3`, with `supersedes`), then close the old open tasks `cancelled` with `result: "Superseded by lino/v3/*"`. Do not leave dead tasks pending |
| **Exclusive resource** | a shared environment or quota is not a task, so the queue cannot lock it. Model it as a chain: users of the resource depend on each other in sequence |

## 6. Decomposition

Plan top-down and claim at the leaves. Each level has a home in Noted:

| level | what it is | where it lives | example |
|---|---|---|---|
| 0. Product | the whole thing | `project` | `lino` |
| 1. Plan | one planning pass over it | key segment | `lino/v2/…` |
| 2. Milestone | a stage that ships something testable end-to-end | `milestone` field, plus a gate task | `"3. Live process"` |
| 3. Component | a package, module, service or command | a task, or a `parent_id` umbrella over its units | `F01 Go module and repo skeleton` |
| 4. Unit | one function, type, endpoint or command, with its tests | the leaf task an agent claims | `L01 Process id derivation` → `IDFor`, `ValidID` |

**Leaves are level 4 when the design is known, and level 3 when it is not.**
A level-3 leaf leaves the executor room to design, and its result's `api`
becomes the contract that level-4 tasks are written from. A level-4 leaf
leaves no room: the signature is in `api`, the cases are in `done_when`.

### The function-level task

The finest grain is one function (or a small closed set of them) plus its
tests. Write it so the executor makes no design decisions:

```json
{
  "title": "A03 Anchor resolution",
  "milestone": "1. Files",
  "spec": "scope.md: Model > Anchor",
  "do": "Resolve an anchor <line>:<hash> against a file's lines. Valid if line N has that hash; else find the unique line with that hash within ±50 lines and return its number. Ranges: both ends relocate by the same offset and end >= start. Otherwise anchor_mismatch, carrying the current lines of the region.",
  "api": [
    "func anchor.Resolve(lines []string, a Anchor) (int, error)",
    "func anchor.ResolveRange(lines []string, start, end Anchor) (int, int, error)",
    "var anchor.ErrMismatch // wraps the current region"
  ],
  "files": ["internal/anchor/resolve.go", "internal/anchor/resolve_test.go"],
  "needs": ["anchor.Anchor and anchor.LineHash from A02"],
  "out_of_scope": ["printing anchors (read command, F05)"],
  "done_when": [
    "table tests: exact hit; moved within 50 lines; moved 51 lines => ErrMismatch; duplicate hash in window => ErrMismatch; range moved together; range ends moved apart => ErrMismatch",
    "go test ./internal/anchor/ passes"
  ]
}
```

### How fine to cut

A task is the right size when all of these hold:

- one agent finishes it in one session (roughly up to ~10 files, and minutes to
  an hour of work);
- it has its own `done_when` that runs without the tasks after it;
- its result is something another task can **use** (an API, a file, a decision);
- you can write `do` without "and also".

Cut further when it fails any of them. Cut **less** when the pieces would only
make sense together, when two tasks would edit the same function, or when
splitting adds edges without adding parallelism. A trivial change (a rename, a
constant) is part of the task that needs it, not a task of its own.

### Order of cutting

1. **Milestones**: each ends in something testable end-to-end. Post its gate
   task.
2. **Spikes**: anything that would change the design if a number came out
   differently. High priority, and everything that rests on it depends on it.
3. **Contracts**: interfaces between components, as `api` tasks, so both sides
   can proceed in parallel.
4. **Units**: one per function, type, endpoint or command, each depending only
   on the contracts and units it actually calls.
5. **Verification**: a verify task for anything whose correctness is expensive
   to get wrong; the milestone gate for the rest.

### Splitting a task you already hold

If you claimed a task and it is too big for one session:

- **Nothing depends on it** (it is a leaf): post the children with
  `parent_id=<this id>` and keys `<key>/1`, `<key>/2`, …, plus a join task that
  depends on all of them and carries the original `done_when`. Then close the
  original `done` with `result: {"split_into": [ids], "join": id}`.
- **Other tasks depend on it**: do not split it. Closing it `done` would release
  its dependants before the work exists. Close it `failed` with
  `result: {"error": "too large for one session", "proposed_split": [...]}` so a
  planner re-plans and supersedes it.

## 7. Posting a plan

```
set_task(
  project="lino",
  key="lino/v2/L01",
  task={...},                 # §4
  priority=40,
  depends_on=[9187],          # ids returned by earlier set_task calls
  parent_id=null,
  created_by="agent-planner",
  max_attempts=2
)
```

| outcome | meaning |
|---|---|
| `created` | new task; `task.id` is its id |
| `exists` | this `key` already exists and you got the existing task. Retrying is safe |
| `parent_not_found` | wrong `parent_id` |

- **Post in dependency order.** `depends_on` takes ids, so a predecessor has to
  exist first. Keep a map from code to id as you go (`F03 → 9189`).
- **`priority`** goes per stage, so the critical path drains first. For example:
  spikes 70, foundations and contracts 60, middle layers 50, leaves and polish
  40, gates and verification at the level of what they check.
- **`max_attempts`** of 2-3 for work that can fail transiently (network,
  flaky builds). Leave it `null` where a failure needs a human or a re-plan.
- **`created_by`**: your name, so delegated work and its results can be traced
  back to you.
- After posting, `get_tasks(project=..., status="pending")` and check that the
  first tasks have `waiting_on: 0`. If nothing is ready, there is an edge you
  did not mean.

## 8. Looking around

- `get_tasks(project="lino", status=["pending","in_progress"])`: what is open.
- `get_tasks(project="lino", status="blocked")`: chains stopped behind a failed
  or cancelled task. Fix or re-run the predecessor, or supersede the chain.
- `get_tasks(project="lino", status="in_progress", stale_seconds=900)`: work
  nobody has touched for 15 minutes.
- `get_tasks(parent_id=N)`: the children of an umbrella.
- `get_task(task_id, with_events=true)`: the full task with its dependencies,
  result and journal (who claimed it, lease expiries, retries, blocks).
- Paging: pass the last id you saw as `before_id`. There is no offset.

Statuses: `pending` · `in_progress` · `blocked` · `done` · `failed` · `cancelled`
(terminal: `done`, `failed`, `cancelled`).

The same operations exist over HTTP at `{{BASE_URL}}/api/tasks` (see
`{{BASE_URL}}/docs`), for scripts that are not agents.
