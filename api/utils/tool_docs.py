"""MCP tool descriptions — one source for both transports.

The model reads these texts, and they are the API documentation an agent gets.
They live in one place so the tools and the JSON API cannot
drift apart.
"""

from __future__ import annotations

from api.models.envelope import HARD_ERRORS as _HARD_OUTCOMES

SET_TASK = """Post a task.

`task` is an arbitrary JSON object describing the work. `project` sets the
scope: an agent working in that project will only ever be handed its tasks.
`assignee_id=null` drops the task into the shared pool, where the first free
agent picks it up. `parent_id` links a subtask to its parent. `key` is an
idempotency key: repeating the call with the same key creates no duplicate and
answers outcome="exists".

priority: higher goes out sooner (0 by default).
max_attempts: how many times to try. A failure with attempts left does not
leave the task failed — it returns to the queue after a growing pause; once
attempts run out the task stays failed, and that is the dead letter.
depends_on: ids that must reach done first. While they are open the task is
handed to nobody; if a predecessor fails or is cancelled the task turns
blocked.
Outcomes: created, exists, parent_not_found — the last one comes back as a
result, not as an error, so branch on it."""

GET_TASKS = """List tasks, newest first.

status takes a string or a list (["pending","in_progress"] is everything open).
project narrows to one project, unscoped=true returns only tasks with none.
unassigned=true returns only the shared pool. stale_seconds finds tasks that
have not been updated for N seconds — that is how in_progress work left behind
by a dead agent is found.
before_id is the paging cursor: the next page is everything older than that id.
There is deliberately no offset — new tasks keep arriving while you page, and
an offset would start skipping rows.
The result field is not included in a list; use get_task for it. Outcome: ok."""

GET_TASK = """One task in full, with its result and its dependencies.

with_events=true adds the transition journal: who changed the status and when,
when the task was claimed, returned after a lease expired, or sent for a retry.
Outcomes: ok, not_found."""

SET_STATUS = """Change a task's status and attach a result.

status: pending | in_progress | blocked | done | failed | cancelled.
result is written for success and failure alike (on failed, put the reason
there).

Compare-and-set is on by default: with no `if_status` the server checks that
the task is still in_progress, the only state an executor may report from. A
mismatch answers outcome="status_conflict" with the current state, so two
agents cannot finish the same task. Pass `if_status` to check a different
state, or force=true to write unconditionally. force is for a human at the
dashboard: presented with an agent key it is refused outright with
outcome="forbidden".

A task held by another executor answers outcome="not_owner", so another
agent's work is not closed by mistake. Who you are follows from your key —
assignee_id is read only when the queue is running without keys at all.

If the task has max_attempts and attempts remain, status failed does not leave
it failed: it returns to the queue after a pause, and the answer will carry
status="pending". That is expected.
Outcomes: updated, not_found, status_conflict, not_owner, stale_session."""

CLAIM_TASK = """Atomically take the next task and move it to in_progress.

Dispatch order is a contract: priority descending, then tasks addressed to this
assignee_id before the shared pool, then FIFO. Two agents can never take the
same task. Tasks still inside a post-failure pause and tasks with open
dependencies are not handed out.
project narrows the search strictly: naming a project excludes both other
projects' tasks and tasks with no project at all.
timeout_s=0 takes a task if one is there; timeout_s>0 waits for one to appear
for up to N seconds, which is cheaper than polling. With nothing to wait for
the answer is outcome="empty".

lease_s is the lease length, 300 seconds by default. Unless it is renewed the
task returns to the queue on its own, so a crashed agent's work never hangs.
The other side of that: work past the lease in silence and another agent takes
the task, with both of you doing it. So either call heartbeat as you go or ask
for a lease that covers the worst case. lease_s=0 takes no lease at all.

A session is a second guard, not a replacement for the lease. When your client
sends Mcp-Session-Id the task is held while *either* the lease has not expired
*or* the session is still alive, so letting a lease run out is not on its own
enough to lose the task. The exception is a client that declared itself
self-renewing: it promised to keep its session fresh in the background, so its
silence is read as a dead process and its tasks are released at once.
Outcomes: claimed, empty."""

HEARTBEAT = """Extend a task's lease: "I am alive and still working on it".

Call it periodically while you work, well inside lease_s. Stop calling and the
lease expires, handing the task to another agent. Only your own task can be
extended, and only while it is in_progress. Who you are follows from your key;
assignee_id is read only when the queue is running without keys at all.
Outcomes: updated, not_found, status_conflict, not_owner, stale_session."""

#: Outcomes an agent must see as a tool error rather than as a result.
#: not_found, status_conflict and empty stay out: those are normal answers.
#:
#: There is exactly one such list and it lives in the envelope. Two independent
#: sets had already drifted once: rate_limited was added to the envelope while
#: MCP kept returning it as a quiet answer.
HARD_ERRORS = frozenset(outcome.value for outcome in _HARD_OUTCOMES)
