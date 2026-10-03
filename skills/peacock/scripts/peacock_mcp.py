#!/usr/bin/env python3
"""
Peacock as an MCP server — the plugin surface.

Speaks MCP over stdio in newline-delimited JSON-RPC 2.0, using nothing but the
standard library, so it installs into Claude Code (or any MCP client) with no
package manager involved.

The tool list is generated from `engine.query.COMMANDS`, which means the MCP
tools and the `peacock q` CLI are the same surface by construction and cannot
drift apart as commands are added.

    peacock mcp --repo /path/to/repo

Usage note for stdio servers: stdout carries the protocol and nothing else.
Every diagnostic goes to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import fmt                                     # noqa: E402
from engine.index import build_index, find_index, index_path_for  # noqa: E402
from engine.query import COMMANDS, QueryError, open_index, run     # noqa: E402

# We echo the client's protocol version when we recognise it; this is the
# fallback for clients that don't send one.
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

SERVER_INFO = {"name": "peacock", "version": "1.0.0"}

# Per-command argument schemas layered on top of the shared ones.
EXTRA_SCHEMA = {
    "neighbors":  {"depth": "int", "kinds": "str", "direction": "dir"},
    "subgraph":   {"depth": "int", "kinds": "str"},
    "impact":     {"depth": "int"},
    "deps":       {"transitive": "bool"},
    "dependents": {"transitive": "bool"},
    "span":       {"context": "int"},
    "find":       {"kind": "str"},
    "hubs":       {"limit": "int"},
}

_PROP = {
    "int": {"type": "integer"},
    "bool": {"type": "boolean"},
    "str": {"type": "string"},
    "dir": {"type": "string", "enum": ["in", "out", "both"]},
}

_HINTS = {
    "depth": "How many hops to walk. Higher costs more tokens.",
    "kinds": "Edge kinds to traverse, comma-separated: calls, imports, contains.",
    "direction": "Walk edges outward, inward, or both.",
    "transitive": "Follow the full closure instead of direct edges only.",
    "context": "Extra source lines to include on each side.",
    "kind": "Restrict to a node kind: fn, class, file, dir, lib.",
    "limit": "Maximum rows to consider.",
}


def tool_name(cmd):
    return cmd.replace("-", "_")


def build_tool_list():
    tools = [{
        "name": "index",
        "description": (
            "Build or refresh the Peacock index for a repository. Run this once "
            "before querying, and again after code changes — it is incremental, "
            "so a re-index after editing one file takes well under a second."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "Repository root. Defaults to the "
                                        "server's configured repo."},
                "force": {"type": "boolean",
                          "description": "Rebuild from scratch, ignoring the "
                                         "parse cache."},
            },
            "required": [],
        },
    }]

    for cmd in sorted(COMMANDS):
        _, _, params, doc = COMMANDS[cmd]
        props, required = {}, []
        if "target" in params:
            props["target"] = {
                "type": "string",
                "description": "A symbol name, path:name, file path, or node id.",
            }
            required.append("target")
        if "seeds" in params:
            props["seeds"] = {
                "type": "array", "items": {"type": "string"},
                "description": "One or more symbols or files to grow the slice "
                               "around.",
            }
            required.append("seeds")
        for arg, kind in EXTRA_SCHEMA.get(cmd, {}).items():
            props[arg] = dict(_PROP[kind], description=_HINTS[arg])
        props["budget"] = {
            "type": "integer",
            "description": f"Max response size in tokens (default "
                           f"{fmt.DEFAULT_BUDGET}). Answers are truncated to "
                           f"fit and say so.",
        }
        tools.append({
            "name": tool_name(cmd),
            "description": doc,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required},
        })
    return tools


class Server:
    def __init__(self, repo=None, index=None, auto_index=True):
        self.repo = os.path.abspath(repo) if repo else None
        self.index = index
        self.auto_index = auto_index
        self.tools = build_tool_list()
        self._by_tool = {tool_name(c): c for c in COMMANDS}

    # -- index lifecycle ---------------------------------------------------- #
    def _index_path(self):
        if self.index:
            return self.index
        if self.repo:
            p = index_path_for(self.repo)
            return p if os.path.exists(p) else None
        return find_index()

    def _ensure_index(self):
        """Build the index on first use if the server was pointed at a repo.

        An agent should not have to know that an index is a thing that must
        exist before it can ask a question.
        """
        path = self._index_path()
        if path:
            return path
        if self.repo and self.auto_index:
            log(f"no index found — building one for {self.repo}")
            build_index(self.repo, log=log)
            return index_path_for(self.repo)
        raise QueryError(
            "no Peacock index found. Call the 'index' tool first, or start the "
            "server with --repo <path>.")

    def do_index(self, args):
        repo = args.get("repo") or self.repo
        if not repo:
            raise QueryError("no repo given and the server has no default; "
                             "pass repo, or start with --repo <path>.")
        st = build_index(repo, force=bool(args.get("force")), log=log)
        return (f"Indexed {st['name']}: {st['code_files']} code files, "
                f"{st['total_loc']:,} LOC, {st['functions']} functions, "
                f"{st['nodes']} nodes / {st['edges']} edges in {st['took']}s "
                f"(parsed {st['parsed']}, reused {st['cached']} cached).\n"
                f"Index: {st['db']}\n"
                f"Start with the 'overview' tool.")

    def do_query(self, cmd, args):
        db = self._ensure_index()
        con = open_index(db)
        try:
            budget = int(args.get("budget") or fmt.DEFAULT_BUDGET)
            kwargs = {k: v for k, v in args.items()
                      if k != "budget" and v is not None}
            _, text = run(con, cmd, budget=budget, **kwargs)
            return text or "(no results)"
        finally:
            con.close()

    # -- protocol ----------------------------------------------------------- #
    def handle(self, msg):
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            return ok(mid, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Peacock answers structural questions about a codebase from "
                    "a precomputed graph. Prefer these tools over grepping and "
                    "reading whole files: start with 'overview', locate with "
                    "'find', read a file's shape with 'outline', trace with "
                    "'who_calls'/'calls', and load task context with 'subgraph'. "
                    "Read source only for the specific spans you need."),
            })

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return ok(mid, {})

        if method == "tools/list":
            return ok(mid, {"tools": self.tools})

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "index":
                    text = self.do_index(args)
                elif name in self._by_tool:
                    text = self.do_query(self._by_tool[name], args)
                else:
                    return err(mid, -32602, f"unknown tool: {name}")
            except QueryError as e:
                return ok(mid, {"content": [{"type": "text", "text": str(e)}],
                                "isError": True})
            except Exception as e:                       # noqa: BLE001
                log(traceback.format_exc())
                return ok(mid, {"content": [{"type": "text",
                                             "text": f"{type(e).__name__}: {e}"}],
                                "isError": True})
            return ok(mid, {"content": [{"type": "text", "text": text}]})

        if mid is None:
            return None
        return err(mid, -32601, f"method not found: {method}")


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def log(*a):
    print("[peacock-mcp]", *a, file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="peacock mcp",
        description="Serve the Peacock code graph to an agent over MCP (stdio).")
    ap.add_argument("--repo", default=None,
                    help="repository root; the index is built here on demand")
    ap.add_argument("--index", default=None, help="explicit path to index.db")
    ap.add_argument("--no-auto-index", action="store_true",
                    help="never build an index implicitly")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    server = Server(repo=a.repo, index=a.index, auto_index=not a.no_auto_index)
    log(f"ready ({len(server.tools)} tools)"
        + (f", repo={server.repo}" if server.repo else ""))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            emit(err(None, -32700, "parse error"))
            continue

        # A batch is a legal JSON-RPC payload, and so is a bare scalar from a
        # confused client. Previously either one raised inside the error
        # handler itself — which called msg.get() on a list — and killed the
        # server for the rest of the session.
        batch = isinstance(msg, list)
        items = msg if batch else [msg]
        replies = []
        for item in items:
            if not isinstance(item, dict):
                replies.append(err(None, -32600, "invalid request"))
                continue
            try:
                r = server.handle(item)
            except Exception as e:                        # noqa: BLE001
                log(traceback.format_exc())
                r = err(item.get("id"), -32603, f"internal error: {e}")
            if r is not None:
                replies.append(r)
        if batch:
            if replies:
                emit(replies)
        elif replies:
            emit(replies[0])
    return 0


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
