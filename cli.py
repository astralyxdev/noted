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
    scope = ", ".join(issued.agent.projects) if issued.agent.projects else "любые проекты"
    print(f"агент:  {issued.agent.id}")
    print(f"скоуп:  {scope}")
    print(f"ключ:   {issued.key}")
    print()
    print("Ключ больше не показывается. Передайте его агенту:")
    print(f"  claude mcp add --transport http noted http://127.0.0.1:8787/mcp/ \\")
    print(f'      --header "X-Noted-Token: {issued.key}"')
    return 0


def _list(args: argparse.Namespace) -> int:
    rows = agents.list_agents(include_revoked=args.all)
    if not rows:
        print("ключей нет — ядро работает как открытая очередь")
        return 0
    width = max(len(a.id) for a in rows)
    for agent in rows:
        scope = ", ".join(agent.projects) if agent.projects else "любые"
        state = "отозван" if agent.revoked_at else "активен"
        alive = len(agents.sessions_of(agent.id))
        print(f"{agent.id:<{width}}  {state:<8}  скоуп: {scope:<24} сессий: {alive}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    if agents.revoke(args.name):
        print(f"ключ {args.name} отозван, его сессии закрыты")
        return 0
    print(f"агент {args.name} не найден или уже отозван", file=sys.stderr)
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="noted-keys", description="Ключи агентов noted")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="завести агента и выдать ключ")
    add.add_argument("name", help="имя агента, оно же assignee_id")
    add.add_argument("--projects", help="скоуп через запятую; без него — любые проекты")
    add.add_argument("--admin", action="store_true", help="ключ с правами администратора")
    add.set_defaults(handler=_add)

    listing = commands.add_parser("list", help="показать агентов")
    listing.add_argument("--all", action="store_true", help="включая отозванных")
    listing.set_defaults(handler=_list)

    revoke = commands.add_parser("revoke", help="отозвать ключ")
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
