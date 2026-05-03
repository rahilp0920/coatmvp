"""`coat` — top-level CLI for the Coat MVP.

Usage:
    python -m cli.coat init
    python -m cli.coat agent onboard [--from-file FILE] [--id ID] [--auto-yes]
    python -m cli.coat agent list
    python -m cli.coat agent show <agent_id>
    python -m cli.coat agent revoke <agent_id>
    python -m cli.coat audit --entity <entity_id> [--since <duration>]   (slice 5)

This file is the single entry point. Subcommands live in cli/<name>.py and
register themselves through the dispatcher below.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make sibling modules importable when run as `python -m cli.coat`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli import agent_onboard, agent_list, agent_grant, coat_audit, coat_init, coat_watch  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="coat", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ---- init / watch ----
    sub.add_parser(
        "init",
        help="initialize Coat in this workspace (build DB, run discovery, render catalog)",
    )
    watch = sub.add_parser(
        "watch",
        help="live tail of WORKFLOW_OBS + capability grants (run in a side pane during demo)",
    )
    watch.add_argument("--poll", type=float, default=0.6,
                       help="poll interval in seconds (default 0.6)")

    # ---- agent ----
    agent = sub.add_parser("agent", help="agent registration + lifecycle")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)

    onboard = agent_sub.add_parser(
        "onboard",
        help="onboard an external agent from a plain-English description",
    )
    onboard.add_argument(
        "--from-file",
        help="read description from a file instead of prompting",
    )
    onboard.add_argument(
        "--id",
        help="explicit agent id (default: auto-generated from description)",
    )
    onboard.add_argument(
        "--provider",
        help="LLM provider hint to record on the agent (anthropic/openai/google)",
    )
    onboard.add_argument(
        "--model",
        help="model identifier hint to record on the agent",
    )
    onboard.add_argument(
        "--auto-yes",
        action="store_true",
        help="skip interactive ratification (for tests / scripted demos)",
    )

    listc = agent_sub.add_parser("list", help="list registered agents")
    listc.add_argument("--show-scopes", action="store_true")

    show = agent_sub.add_parser("show", help="show one agent's manifest")
    show.add_argument("agent_id")

    revoke = agent_sub.add_parser("revoke", help="revoke an agent (status → revoked)")
    revoke.add_argument("agent_id")
    revoke.add_argument("--reason", default="manual revoke")

    grant = agent_sub.add_parser(
        "grant",
        help="grant an additional scope to a registered agent (audited)",
    )
    grant.add_argument("agent_id")
    grant.add_argument("scope")
    grant.add_argument("--by", dest="granted_by", default="u_mgr_c",
                       help="granter user_id (recorded on the audit row)")
    grant.add_argument("--reason", default=None)
    grant.add_argument("--auto-yes", action="store_true",
                       help="skip the interactive ratification prompt")

    revoke_scope = agent_sub.add_parser(
        "revoke-scope",
        help="revoke a single scope from an agent (the agent itself stays active)",
    )
    revoke_scope.add_argument("agent_id")
    revoke_scope.add_argument("scope")
    revoke_scope.add_argument("--reason", default=None)

    # ---- audit ----
    audit = sub.add_parser("audit", help="entity timeline view of WORKFLOW_OBS + capability provenance")
    audit.add_argument("--entity", required=True,
                       help="entity to filter on (e.g. SKU-441, V1001, atlas@coat.io/v1)")
    audit.add_argument("--since", default="24h",
                       help="duration window: 1h / 24h / 7d / 30d (default 24h)")

    args = parser.parse_args()

    if args.cmd == "init":
        coat_init.init()
        return
    if args.cmd == "watch":
        coat_watch.watch(poll_seconds=args.poll)
        return
    if args.cmd == "agent":
        if args.agent_cmd == "onboard":
            agent_onboard.run(
                from_file=args.from_file,
                explicit_id=args.id,
                provider=args.provider,
                model=args.model,
                auto_yes=args.auto_yes,
            )
        elif args.agent_cmd == "list":
            agent_list.list_agents(show_scopes=args.show_scopes)
        elif args.agent_cmd == "show":
            agent_list.show_agent(args.agent_id)
        elif args.agent_cmd == "revoke":
            agent_list.revoke_agent(args.agent_id, reason=args.reason)
        elif args.agent_cmd == "grant":
            agent_grant.grant(
                args.agent_id, args.scope,
                granted_by=args.granted_by, reason=args.reason,
                auto_yes=args.auto_yes,
            )
        elif args.agent_cmd == "revoke-scope":
            agent_grant.revoke_scope(args.agent_id, args.scope, reason=args.reason)
        else:
            parser.error(f"unknown agent subcommand {args.agent_cmd!r}")
    elif args.cmd == "audit":
        coat_audit.audit_entity(args.entity, since=args.since)
    else:
        parser.error(f"unknown command {args.cmd!r}")


if __name__ == "__main__":
    main()
