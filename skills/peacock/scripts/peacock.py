#!/usr/bin/env python3
"""Peacock's dependency-free index, query, and MCP command line."""
from __future__ import annotations

import argparse
import json
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def status(message=""):
    """Write progress separately from query and JSON output."""
    print(message, file=sys.stderr, flush=True)


def cmd_index(argv):
    from engine.index import build_index

    parser = argparse.ArgumentParser(
        prog="peacock index",
        description="Build or update the agent-queryable index for a repository.",
    )
    parser.add_argument("repo", nargs="?", default=".", help="repository root")
    parser.add_argument(
        "--out", default=None, help="index path (default: <repo>/.peacock/index.db)"
    )
    parser.add_argument(
        "--force", action="store_true", help="rebuild instead of using the parse cache"
    )
    parser.add_argument(
        "--all", action="store_true", help="include files ignored by Git"
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    emit = (lambda *_: None) if (args.quiet or args.json) else status
    try:
        result = build_index(
            args.repo,
            out=args.out,
            force=args.force,
            respect_gitignore=not args.all,
            log=emit,
        )
    except (NotADirectoryError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif not args.quiet:
        print(
            f"🦚 Indexed {result['name']} — {result['code_files']} code files, "
            f"{result['total_loc']:,} LOC, {result['nodes']} nodes / "
            f"{result['edges']} edges in {result['took']}s"
        )
        print(
            f"   {result['db']} (parsed {result['parsed']}, "
            f"cached {result['cached']}, dropped {result['removed']})"
        )
        print("   try: peacock q overview")
    return 0


def cmd_query(argv):
    from engine import fmt
    from engine.query import COMMANDS, QueryError, open_index, run

    parser = argparse.ArgumentParser(
        prog="peacock q",
        description="Query the code graph with context-budgeted answers.",
        epilog="commands: " + ", ".join(sorted(COMMANDS)),
    )
    parser.add_argument("command", nargs="?", help="query command")
    parser.add_argument("args", nargs="*", help="command target(s)")
    parser.add_argument("--index", default=None, help="explicit index.db path")
    parser.add_argument("--budget", type=int, default=fmt.DEFAULT_BUDGET)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument("--kinds", default=None)
    parser.add_argument("--kind", default=None)
    parser.add_argument("--direction", default="both", choices=("in", "out", "both"))
    parser.add_argument("--transitive", action="store_true")
    parser.add_argument("--context", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if not args.command or args.command in ("help", "--help"):
        print("peacock q <command> [target...] [--budget N] [--json]\n")
        for name in sorted(COMMANDS):
            _, _, params, doc = COMMANDS[name]
            signature = " ".join(f"<{param}>" for param in params)
            print(f"  {name:<11}{signature:<10} {doc}")
        return 0

    try:
        connection = open_index(args.index)
    except QueryError as exc:
        raise SystemExit(f"Error: {exc}") from exc

    _, _, params, _ = COMMANDS.get(args.command, (None, None, [], None))
    kwargs = {}
    if "target" in params:
        if not args.args:
            raise SystemExit(f"Error: '{args.command}' needs a target")
        kwargs["target"] = args.args[0]
    if "seeds" in params:
        if not args.args:
            raise SystemExit(f"Error: '{args.command}' needs at least one seed")
        kwargs["seeds"] = args.args
    if args.depth is not None:
        kwargs["depth"] = args.depth
    if args.kinds:
        kwargs["kinds"] = args.kinds
    if args.kind:
        kwargs["kind"] = args.kind
    if args.direction != "both":
        kwargs["direction"] = args.direction
    if args.transitive:
        kwargs["transitive"] = True
    if args.context:
        kwargs["context"] = args.context
    if args.limit != 20:
        kwargs["limit"] = args.limit

    try:
        data, output = run(connection, args.command, budget=args.budget, **kwargs)
    except QueryError as exc:
        raise SystemExit(f"Error: {exc}") from exc
    finally:
        connection.close()

    print(json.dumps(data, indent=2, default=str) if args.json else output)
    return 0


def cmd_mcp(argv):
    import peacock_mcp

    return peacock_mcp.main(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            "Peacock 🦚 — compact codebase intelligence for agents\n\n"
            "Commands:\n"
            "  peacock index [repo]    Build/update the local graph\n"
            "  peacock q <command>     Query the graph\n"
            "  peacock mcp             Serve the graph over MCP stdio"
        )
        return 0
    command, rest = argv[0], argv[1:]
    if command == "index":
        return cmd_index(rest)
    if command in ("q", "query"):
        return cmd_query(rest)
    if command == "mcp":
        return cmd_mcp(rest)
    raise SystemExit(f"Unknown command: {command}. Try: peacock --help")


if __name__ == "__main__":
    raise SystemExit(main())
