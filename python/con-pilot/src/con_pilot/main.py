"""
main.py — CLI entry point for con-pilot.

Usage
-----
  con-pilot sync                   Run one sync + cron cycle and exit.
  con-pilot cron                   Dispatch cron jobs only and exit.
  con-pilot serve [-i SECONDS]     Run the FastAPI service.
  con-pilot setup-env [--shell]    Print session env vars and start watcher.
  con-pilot register NAME DIR      Register a new project.
  con-pilot retire-project NAME    Retire a project.
  con-pilot replace FILE ROLE [PROJECT] [--key KEY]
                                   Replace agent body with instructions file.
  con-pilot reset ROLE [PROJECT] [--key KEY]
                                   Reset agent(s) to template / default.
"""

import argparse
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[con-pilot %(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="con-pilot",
        description="Conductor pilot — agent sync and cron scheduler.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("help", help="Show this help message and exit.")

    sub.add_parser(
        "sync", help="Run one full sync cycle (agent reconcile + cron dispatch) and exit."
    )
    sub.add_parser("cron", help="Dispatch cron jobs only and exit.")

    serve_p = sub.add_parser("serve", help="Run the FastAPI service as a continuous server.")
    serve_p.add_argument(
        "-i",
        "--interval",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override the sync interval in seconds (default: 900).",
    )

    env_p = sub.add_parser(
        "setup-env",
        help="Print session env vars derived from conductor.json (KEY=VALUE per line).",
    )
    env_p.add_argument(
        "--shell",
        action="store_true",
        help="Output as 'export KEY=\"VALUE\"' lines suitable for eval in bash.",
    )

    reg_p = sub.add_parser(
        "register",
        help="Register a new project: update trust.json and create its agent files.",
    )
    reg_p.add_argument("name", help="Project name (e.g. my-app).")
    reg_p.add_argument("directory", help="Absolute path to the project root directory.")

    retire_p = sub.add_parser(
        "retire-project",
        help="Retire a project: archive its directory and remove it from trust.json.",
    )
    retire_p.add_argument("name", help="Project name to retire.")

    def _add_key_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--key",
            default=None,
            metavar="KEY",
            help="System key required when editing system-scoped agents.",
        )

    # amend command disabled — pending implementation
    # amend_p = sub.add_parser(
    #     "amend",
    #     help="Append/merge an ## Instructions section into matching agent file(s).",
    # )
    # amend_p.add_argument("file", help="Path to the instructions file.")
    # amend_p.add_argument("role", help="Agent role type (e.g. developer, reviewer).")
    # amend_p.add_argument("project", nargs="?", default=None, help="Project name (optional).")
    # _add_key_arg(amend_p)

    replace_p = sub.add_parser(
        "replace",
        help="Replace agent body entirely with the content of an instructions file.",
    )
    replace_p.add_argument("file", help="Path to the instructions file.")
    replace_p.add_argument("role", help="Agent role type (e.g. developer, reviewer).")
    replace_p.add_argument("project", nargs="?", default=None, help="Project name (optional).")
    _add_key_arg(replace_p)

    reset_p = sub.add_parser(
        "reset",
        help="Reset agent(s) to their template / default generated content.",
    )
    reset_p.add_argument("role", help="Agent role type (e.g. developer, reviewer).")
    reset_p.add_argument("project", nargs="?", default=None, help="Project name (optional).")
    _add_key_arg(reset_p)

    args = parser.parse_args()

    if args.command is None or args.command == "help":
        parser.print_help()
        raise SystemExit(0)

    from con_pilot.conductor import ConPilot  # noqa: PLC0415

    pilot = ConPilot()

    if args.command == "setup-env":
        pilot.print_env(shell=args.shell)
    elif args.command == "sync":
        pilot.sync()
    elif args.command == "cron":
        pilot.cron()
    elif args.command == "serve":
        pilot.serve(interval=args.interval)
    elif args.command == "register":
        pilot.register(args.name, args.directory)
    elif args.command == "retire-project":
        pilot.retire_project(args.name)
    # amend disabled — pending implementation
    # elif args.command == "amend":
    #     pilot.amend_agent(args.file, args.role, args.project, args.key)
    elif args.command == "replace":
        pilot.replace_agent(args.file, args.role, args.project, args.key)
    elif args.command == "reset":
        pilot.reset_agent(args.role, args.project, args.key)


if __name__ == "__main__":
    main()
