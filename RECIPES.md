# Recipes

Noted is a primitive: a queue where a task goes to exactly one holder, survives
that holder's death, and remembers what happened to it. Everything else —
acceptance, isolating working copies, launching agents, the shape of a payload —
is assembled on top and stays with whoever writes the orchestration.

What follows shows the primitive is enough for that.

## Acceptance: a verifier task

`done` means precisely one thing: the executor said it was done. LLM agents
close unfinished work with confidence, and there is deliberately no review
status in the core — the moment you add one you also need "who reviews", "how
many reviews" and "what happens on disagreement".

Review is assembled from dependencies: the work and its acceptance are two
tasks, and the second waits for the first.

```python
work = set_task(
    task={"goal": "Fix date parsing in the report",
          "acceptance": ["pytest tests/test_report.py", "dates in UTC"]},
    project="noted", max_attempts=2,
)["task"]

verify = set_task(
    task={"goal": "Check acceptance", "verify_task": work["id"],
          "acceptance": ["tests green", "the diff does not touch migrations"]},
    project="noted", depends_on=[work["id"]],
)["task"]
```

The verifier is handed to nobody until the work is `done`. A **different** agent
takes it — the same one would say "done" again. Then there are two outcomes:

- acceptance passed: the verifier closes with `done` and the chain is complete;
- acceptance failed: the verifier queues the rework and closes:

```python
set_status(task_id=verify["id"], status="done",
           result={"verdict": "rejected", "why": "tests are red"})
set_task(task={"goal": "Address the review", "after": verify["id"], "notes": "…"},
         project="noted", priority=5)
```

Why the verifier does not use `failed`: a failure means the **check** could not
be carried out, not that the work is bad. A verdict is a result, not a status.

## Isolating the repository: a branch in the payload

Noted separates agents by task, not by file. Two agents on different tasks
editing the same file will collide in the working copy, and the queue has
nothing to do with it.

The answer is not to let them into one working copy. The orchestrator allocates
a branch and a worktree per task and puts both in the payload:

```python
set_task(task={
    "goal": "Add CSV export",
    "repo": "git@github.com:astralyxdev/noted.git",
    "branch": "task/412-csv-export",
    "worktree": "/var/agents/wt/412",
    "base": "main",
})
```

The executor works only inside its own worktree and hands back a branch rather
than changes in a shared copy:

```python
set_status(task_id=412, status="done",
           result={"branch": "task/412-csv-export", "commit": "a1b2c3d"})
```

Merging is a separate task with `depends_on` on every branch that has to come
together. A conflict then becomes visible work in the queue instead of silent
damage to the tree.

## The shape of a payload

`task` is arbitrary JSON on purpose: the core does not know what your agents
do. But executors written by different people will not understand each other
unless the format is agreed. A working minimum:

```json
{
  "goal": "one sentence: what must become true",
  "acceptance": ["checkable conditions the work is accepted by"],
  "context": {"files": ["api/routes/tasks.py"], "issue": 412},
  "artifacts": {"branch": "task/412-csv-export"}
}
```

Keep `goal` and `acceptance` mandatory in your own orchestration: without
acceptance criteria the verifier from the first recipe has nothing to stand on.

## A supervisor: who keeps agents alive

Noted launches nobody — it hands work to whoever turns up. Keeping live
executors around is somebody else's job. A minimal supervisor in bash: N
processes, each on a long poll.

```bash
#!/usr/bin/env bash
# supervise.sh — keeps N agents on a project
set -euo pipefail
PROJECT=${1:?project}
COUNT=${2:-4}

for i in $(seq 1 "$COUNT"); do
  (
    while true; do
      claude -p "Take a task with claim_task(project='$PROJECT', timeout_s=60) and do it.
                 Report back with set_status and a result. If the queue is empty, just exit."
      sleep 1   # empty queue: do not spin
    done
  ) &
done
wait
```

`timeout_s=60` is the long poll: the process sleeps on a connection instead of
polling the queue. An empty queue costs one hanging request a minute, not sixty.

In production replace `&` with a systemd unit or `docker compose --scale`, so a
crashed agent comes back by itself. The session of a dead process expires and
its tasks return to the queue without your involvement.

Prefer the stdio adapter for work of this shape. It renews its session in the
background while the model is busy, so a long step cannot lose the task; over
the HTTP transport nothing is sent during that step, and only `lease_s` (or an
explicit `heartbeat`) keeps the task from being handed to somebody else.

## Integrations: events instead of polling

The `/events` stream is a public contract with a cursor. A client remembers the
number of the last event and, after a disconnect, asks to continue from it — so
external logic can be built on events rather than on lighting up the dashboard.

```python
import json, httpx

cursor = 0
while True:
    with httpx.stream("GET", "http://127.0.0.1:8787/events",
                      params={"after": cursor}, timeout=None) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            cursor = event["id"]

            if event["event"] == "status" and event["to_status"] == "done":
                notify_chat(f"task {event['task_id']} closed by {event['actor']}")
            if event["event"] == "dead_letter":
                page_oncall(event["task_id"])
```

Launching an agent for a task that just appeared (`event == "created"`) and
wiring the queue to a chat are built the same way. None of it belongs in the
core — reading the stream is enough.

## What recipes cannot cover

- **One machine.** The core is a single process: if it falls, the swarm stops.
  There is no replication.
- **Rights inside the dashboard.** There is one door and no roles: whoever signs
  in is an admin.
- **Contention over external resources.** If agents need exclusivity over
  something other than a task — a shared environment, an API quota — the queue
  knows nothing about it. Model it as a lock task with `depends_on`.
