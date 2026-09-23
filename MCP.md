# MCP

The tool surface an agent sees. The server is at `/mcp` on the same port as
everything else, so nothing has to be installed or started beside the core:

```bash
claude mcp add --transport http noted http://127.0.0.1:8787/mcp/
```

There is no authentication. Anything that reaches the port has full use of the
queue — see **Identity** in [SPEC.md](SPEC.md) for why, and what follows from it.

## How to read an answer

Every tool returns an envelope, and the field to branch on is `outcome`:

```json
{"ok": true, "outcome": "claimed", "message": null, "task": {"id": 42, "...": "..."}}
```

`not_found`, `status_conflict` and `empty` are **normal answers**, not failures —
an empty queue is not an error, and losing a compare-and-set is information.
Malformed input, a rate limit and internal faults arrive as MCP tool errors
instead, because there is nothing sensible for an agent to do with them.

The tables below are generated from the running server's own schema; the
`Answers` line lists the outcomes each tool can return as a result.

## The tools

### `set_task`

Post a task.

| parameter | type | required | default |
|---|---|---|---|
| `task` | object | **yes** | — |
| `project` | string or null | no | `null` |
| `assignee_id` | string or int or null | no | `null` |
| `parent_id` | int or null | no | `null` |
| `key` | string or null | no | `null` |
| `created_by` | string or int or null | no | `null` |
| `priority` | int | no | `0` |
| `max_attempts` | int or null | no | `null` |
| `depends_on` | array&lt;int&gt; or null | no | `null` |

Answers: `created`, `exists`, `parent_not_found`

### `claim_task`

Atomically take the next task and move it to in_progress.

| parameter | type | required | default |
|---|---|---|---|
| `assignee_id` | string or int | **yes** | — |
| `project` | string or null | no | `null` |
| `timeout_s` | number | no | `0` |
| `lease_s` | number or null | no | `null` |

Answers: `claimed`, `empty`

### `set_status`

Change a task's status and attach a result.

| parameter | type | required | default |
|---|---|---|---|
| `task_id` | int | **yes** | — |
| `status` | string | **yes** | — |
| `result` | any | no | `null` |
| `if_status` | string or null | no | `null` |
| `assignee_id` | string or int or null | no | `null` |
| `force` | bool | no | `false` |

Answers: `updated`, `not_found`, `status_conflict`, `not_owner`

### `heartbeat`

Extend a task's lease: "I am alive and still working on it".

| parameter | type | required | default |
|---|---|---|---|
| `task_id` | int | **yes** | — |
| `assignee_id` | string or int | **yes** | — |
| `lease_s` | number or null | no | `null` |

Answers: `updated`, `not_found`, `status_conflict`, `not_owner`

### `get_tasks`

List tasks, newest first.

| parameter | type | required | default |
|---|---|---|---|
| `assignee_id` | string or int or null | no | `null` |
| `status` | string or array&lt;string&gt; or null | no | `null` |
| `project` | string or null | no | `null` |
| `unscoped` | bool | no | `false` |
| `unassigned` | bool | no | `false` |
| `parent_id` | int or null | no | `null` |
| `stale_seconds` | number or null | no | `null` |
| `before_id` | int or null | no | `null` |
| `limit` | int | no | `50` |

Answers: `ok`

### `get_task`

One task in full, with its result and its dependencies.

| parameter | type | required | default |
|---|---|---|---|
| `task_id` | int | **yes** | — |
| `with_events` | bool | no | `false` |

Answers: `ok`, `not_found`

## What the parameters mean in practice

**`assignee_id` is a name you choose, not a credential.** Nothing verifies it.
It decides which tasks you can see — yours plus the unaddressed pool — and it is
what `not_owner` compares against when you report. Two agents using the same
name are one holder as far as the queue is concerned, so make it unique in
whatever spawns them.

**The lease is the only thing holding your task.** `claim_task` takes one:
`lease_s: null` means the default 300 seconds, and `lease_s: 0` means no expiry
at all — nothing will ever take the task back, including if you die. A model
makes no tool calls while it is working, so either ask for a lease that covers
your longest step, or call `heartbeat` between steps.

`heartbeat` without `lease_s` resets to the default. On a task claimed with
`lease_s: 0` that ends the opt-out and gives it an expiry it did not have — pass
`0` again to keep it.

**`set_status` checks before it writes.** With no `if_status` the server
requires the task to still be `in_progress`, which is what stops you overwriting
a cancellation somebody made while you worked. `force: true` skips the check;
it is meant for a human at the dashboard, and nothing prevents an agent using
it, so treat it as off-limits.

Reporting `failed` on a task with `max_attempts` left does not leave it failed —
it returns to the queue after a growing pause, and the answer carries
`status: "pending"`. That happens with `force: true` as well.

**Dispatch order is a contract**: priority descending, then tasks addressed to
you before the shared pool, then oldest first. Naming a `project` is strict —
you get neither another project's tasks nor unaddressed ones.

**`timeout_s` is a long poll, not polling.** The connection waits for work to
appear and returns the moment it does. It is capped at 300 seconds.

## A minimal executor

```
claim_task(assignee_id="agent-1", project="noted", timeout_s=60)
  → {"outcome": "claimed", "task": {"id": 42, "task": {...}}}

  ... do the work ...

set_status(task_id=42, status="done", result={"passed": 40})
  → {"outcome": "updated"}
```

An `empty` answer means the queue had nothing within `timeout_s`. Claim again.

More patterns — verification tasks, worktrees in the payload, supervisors,
reacting to the event stream — are in [RECIPES.md](RECIPES.md).
