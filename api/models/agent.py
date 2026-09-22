"""Principal schemas: an agent and its session."""

from __future__ import annotations

from pydantic import BaseModel

#: What a client puts in `X-Noted-Transport` to say that it renews its session
#: in the background, without the model having to think about it.
#:
#: This is the one thing the collector cannot work out for itself. An HTTP
#: client sends nothing at all during a long step, so its silence proves
#: nothing and only the lease can govern the hold. A client that renews itself
#: is different: if it has gone quiet, the process is gone, and its tasks can
#: go back to the queue immediately instead of waiting out a lease.
SELF_RENEWING = "self-renewing"


class Agent(BaseModel):
    """An agent as a principal. `id` is both the name and the `assignee_id` on tasks."""

    id: str
    projects: list[str] | None = None
    is_admin: bool = False
    created_at: str
    revoked_at: str | None = None

    def may_touch(self, project: str | None) -> bool:
        """Scope is enforced, not advisory: `projects=None` means any.

        Tasks outside every project stay open to everyone — a shared pool is
        shared on purpose.
        """
        if self.projects is None or project is None:
            return True
        return project in self.projects


class AgentSession(BaseModel):
    """One running instance of an agent. Lives for as long as it is renewed."""

    id: str
    agent_id: str
    transport: str | None = None
    opened_at: str
    renewed_at: str
    closed_at: str | None = None


class IssuedKey(BaseModel):
    """The key is shown exactly once; only its hash is stored."""

    agent: Agent
    key: str
