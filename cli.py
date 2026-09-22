"""Agent key management.

A key is shown once when issued; only its hash is stored. Issuing from the
command line rather than from the browser is deliberate: the dashboard is open
to anyone who can reach the port, and it has no business handing out keys.
"""

from __future__ import annotations

import argparse
import sys

from api.services import agents
from api.services.agents import AgentError


def _add(args: argparse.Namespace) -> int:
    projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()]
    issued = agents.issue(args.name, projects=projects or None, is_admin=args.admin)
    scope = ", ".join(issued.agent.projects) if issued.agent.projects else "any project"
    print(f"agent:  {issued.agent.id}")
    print(f"scope:  {scope}")
    print(f"key:    {issued.key}")
    print()
    print("The key is never shown again. Hand it to the agent:")
    print("  claude mcp add --transport http noted http://127.0.0.1:8787/mcp/ \\")
    print(f'      --header "X-Noted-Token: {issued.key}"')
    print()
    print("For long steps prefer the stdio adapter: it renews the session while the")
    print("model is busy, so a ten-minute build cannot lose the task.")
    print(f"  claude mcp add noted --env NOTED_KEY={issued.key} -- noted-mcp")
    return 0


def _list(args: argparse.Namespace) -> int:
    rows = agents.list_agents(include_revoked=args.all)
    if not rows:
        print("no keys yet — the core runs as an open queue")
        return 0
    width = max(len(a.id) for a in rows)
    for agent in rows:
        scope = ", ".join(agent.projects) if agent.projects else "any"
        state = "revoked" if agent.revoked_at else "active"
        alive = len(agents.sessions_of(agent.id))
        print(f"{agent.id:<{width}}  {state:<8}  scope: {scope:<24} sessions: {alive}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    if agents.revoke(args.name):
        print(f"key {args.name} revoked, its sessions are closed")
        return 0
    print(f"agent {args.name} not found, or already revoked", file=sys.stderr)
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="noted-keys", description="noted agent keys")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="register an agent and issue a key")
    add.add_argument("name", help="the agent name, which is also its assignee_id")
    add.add_argument("--projects", help="comma-separated scope; omit for any project")
    add.add_argument("--admin", action="store_true", help="issue a key with admin rights")
    add.set_defaults(handler=_add)

    listing = commands.add_parser("list", help="show agents")
    listing.add_argument("--all", action="store_true", help="include revoked ones")
    listing.set_defaults(handler=_list)

    revoke = commands.add_parser("revoke", help="revoke a key")
    revoke.add_argument("name")
    revoke.set_defaults(handler=_revoke)

    args = parser.parse_args()
    try:
        raise SystemExit(args.handler(args))
    except AgentError as error:
        print(error.message, file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
