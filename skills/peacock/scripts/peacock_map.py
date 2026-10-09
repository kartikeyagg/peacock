"""`peacock map`: an AI-readiness scorecard and an interactive browser map.

The map is drawn from the same index `peacock q` answers from, so the picture
and the query answers never disagree. Everything is standard-library Python;
the browser view uses a vendored copy of three.js and needs no network.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
DEFAULT_PORT = 7278

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "magenta": "\033[35m",
}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}

METRIC_NAMES = {
    "navigability": "AI Navigability",
    "context_efficiency": "Context Efficiency",
    "modularity": "Modularity & Coupling",
    "complexity": "Complexity & Maintainability",
    "self_description": "Self-Description (docs/types)",
}


def log(msg=""):
    print(msg, flush=True)


def status(msg=""):
    """Progress output goes to stderr so stdout stays clean for --json."""
    print(msg, file=sys.stderr, flush=True)


def analyze(root, max_nodes=2500, view="nebula", respect_gitignore=True):
    from engine.graph import from_index
    from engine.index import build_index, connect
    from engine.layout import layout_3d, layout_atlas
    from engine.metrics import compute_metrics

    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SystemExit(f"Error: not a directory: {root}")

    t0 = time.time()
    status(f"{C['cyan']}🦚 Scanning{C['reset']} {C['bold']}{root}{C['reset']} ...")
    st = build_index(root, respect_gitignore=respect_gitignore,
                     log=lambda m: status(f"{C['dim']}{m}{C['reset']}"))
    con = connect(st["db"])
    try:
        files, graph = from_index(con)
    finally:
        con.close()
    if not files:
        raise SystemExit("Error: no analyzable source files found.")

    lang_counts = {}
    for fi in files:
        if fi.kind == "code":
            lang_counts[fi.language] = lang_counts.get(fi.language, 0) + 1
    status(f"{C['dim']}   {len(files)} files across {len(lang_counts)} languages "
           f"in {time.time() - t0:.2f}s{C['reset']}")

    status(f"{C['cyan']}🦚 Scoring{C['reset']} ...")
    scores = compute_metrics(files, graph, os.listdir(root))

    graph_out = prune_graph(graph, max_nodes)
    status(f"{C['cyan']}🦚 Laying out {len(graph_out['nodes'])} nodes{C['reset']} ...")
    node_map = {n["id"]: n for n in graph_out["nodes"]}
    pos = layout_3d(node_map, graph_out["edges"])
    # The flat atlas view uses its own packed 2D layout.
    apos, cells = layout_atlas(node_map, graph_out["edges"])
    for n in graph_out["nodes"]:
        n["x"], n["y"], n["z"] = pos.get(n["id"], [0, 0, 0])
        n["ax"], n["ay"] = apos.get(n["id"], [0, 0])
    graph_out["atlas"] = {"cells": cells}

    return {
        "meta": {
            "root": root,
            "name": os.path.basename(root.rstrip("/\\")) or "repository",
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "took": round(time.time() - t0, 2),
            "tool": "Peacock 🦚",
            "view": view,
        },
        "stats": _stats(files, graph, lang_counts),
        "scores": scores,
        "graph": graph_out,
    }


def prune_graph(graph, max_nodes):
    """Keep the most important nodes of a huge repository, always keeping the
    modules and files that give the map its structure."""
    nodes = list(graph.nodes.values())
    if len(nodes) <= max_nodes:
        keep = {n["id"] for n in nodes}
    else:
        must = [n for n in nodes if n["kind"] in ("Module", "File", "Library")]
        classes = sorted((n for n in nodes if n["kind"] == "Class"),
                         key=lambda n: n.get("degree", 0), reverse=True)
        funcs = sorted((n for n in nodes if n["kind"] == "Function"),
                       key=lambda n: n.get("degree", 0), reverse=True)
        budget = max(0, max_nodes - len(must))
        # Reserve up to 30% of the remaining budget for classes so types stay
        # visible, then fill the rest with the busiest functions.
        n_class = min(len(classes), int(budget * 0.30))
        keep = {n["id"] for n in must + classes[:n_class] + funcs[: budget - n_class]}
    return {
        "nodes": [_clean_node(n) for n in nodes if n["id"] in keep],
        "edges": [e for e in graph.edges
                  if e["source"] in keep and e["target"] in keep],
    }


def _clean_node(n):
    keep_keys = ("id", "kind", "label", "path", "language", "loc", "complexity",
                 "line", "length", "documented", "degree", "size", "external",
                 "file_kind")
    return {k: n[k] for k in keep_keys if k in n}


def _stats(files, graph, lang_counts):
    code = [f for f in files if f.kind == "code"]
    kinds, edge_kinds = {}, {}
    for n in graph.nodes.values():
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    for e in graph.edges:
        edge_kinds[e["kind"]] = edge_kinds.get(e["kind"], 0) + 1
    return {
        "files": len(files),
        "code_files": len(code),
        "total_loc": sum(f.loc for f in code),
        "functions": sum(1 for f in code for s in f.symbols if s.kind == "function"),
        "classes": sum(1 for f in code for s in f.symbols if s.kind == "class"),
        "languages": dict(sorted(lang_counts.items(), key=lambda kv: -kv[1])),
        "node_kinds": kinds,
        "edge_kinds": edge_kinds,
        "total_nodes": len(graph.nodes),
        "total_edges": len(graph.edges),
    }


def write_report(data, out_dir):
    """Copy the web template next to a data.json for this repository."""
    os.makedirs(out_dir, exist_ok=True)
    for item in os.listdir(WEB_DIR):
        src, dst = os.path.join(WEB_DIR, item), os.path.join(out_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    return out_dir


def find_vscode():
    """argv prefix for a VS Code-compatible CLI, or None."""
    for name in ("code", "code-insiders", "codium", "cursor"):
        path = shutil.which(name)
        if path:
            return [path]
    for cand in ("/usr/bin/code", "/usr/local/bin/code", "/snap/bin/code",
                 os.path.expanduser("~/.local/bin/code"),
                 "/usr/share/code/bin/code",
                 "/var/lib/flatpak/exports/bin/com.visualstudio.code"):
        if os.path.exists(cand):
            return [cand]
    if shutil.which("flatpak") and os.path.exists("/var/lib/flatpak/app/com.visualstudio.code"):
        return ["flatpak", "run", "com.visualstudio.code"]
    return None


def make_handler(project_root):
    import http.server
    import subprocess
    import urllib.parse

    root = os.path.realpath(project_root) if project_root else None
    Base = http.server.SimpleHTTPRequestHandler

    class PeacockHandler(Base):
        def log_message(self, *args):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _safe_path(self, rel):
            """Resolve rel inside the project root; refuse traversal."""
            if not root:
                return None
            path = os.path.realpath(os.path.join(root, rel))
            try:
                if os.path.commonpath([path, root]) != root:
                    return None
            except ValueError:
                return None
            return path

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self._api()
            return Base.do_GET(self)

        def _api(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            code = find_vscode()

            if parsed.path == "/api/vscode-status":
                return self._json({"available": bool(code), "root": root})
            if not code:
                return self._json({"ok": False, "error": "VS Code CLI not found"})

            if parsed.path == "/api/open-project":
                if not root:
                    return self._json({"ok": False, "error": "no root"})
                try:
                    subprocess.Popen(code + [root])
                    return self._json({"ok": True})
                except OSError as exc:
                    return self._json({"ok": False, "error": str(exc)})

            if parsed.path == "/api/open":
                path = self._safe_path((qs.get("path") or [""])[0])
                line = (qs.get("line") or ["1"])[0]
                if not line.isdigit():
                    line = "1"
                if not path or not os.path.exists(path):
                    return self._json({"ok": False, "error": "file not found"})
                try:
                    subprocess.Popen(code + ["-g", f"{path}:{line}"])
                    return self._json({"ok": True})
                except OSError as exc:
                    return self._json({"ok": False, "error": str(exc)})

            return self._json({"ok": False, "error": "unknown endpoint"}, 404)

    return PeacockHandler


def bind(out_dir, port, project_root=None, attempts=20):
    """A server on the first free port at or above `port`, on loopback only."""
    import functools
    import socketserver

    handler = functools.partial(make_handler(project_root),
                                directory=os.path.abspath(out_dir))
    for p in range(port, port + attempts):
        try:
            return socketserver.ThreadingTCPServer(("127.0.0.1", p), handler)
        except OSError:
            continue
    raise SystemExit("Error: could not bind a local port.")


def serve(out_dir, port, open_browser=True, project_root=None):
    httpd = bind(out_dir, port, project_root)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    log(f"\n{C['green']}{C['bold']}🦚 Peacock map is live:{C['reset']} {C['cyan']}{url}{C['reset']}")
    if find_vscode():
        log(f"{C['dim']}   VS Code integration available: select a node and press "
            f"'o' to open it.{C['reset']}")
    log(f"{C['dim']}   Press Ctrl+C to stop the server.{C['reset']}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log(f"\n{C['dim']}🦚 Stopped.{C['reset']}")
    finally:
        httpd.server_close()


def print_scorecard(data):
    s, st = data["scores"], data["stats"]
    rule = f"{C['bold']}{C['magenta']}" + "═" * 64 + C["reset"]
    log("")
    log(rule)
    log(f"  {C['bold']}🦚 PEACOCK SCORECARD{C['reset']}   {data['meta']['name'][:38]}")
    log(rule)
    ov = s["overall"]
    col = C["green"] if ov >= 78 else C["yellow"] if ov >= 55 else C["red"]
    log(f"  {C['bold']}Overall{C['reset']}   {col}{C['bold']}{ov:>5.1f}{C['reset']} / 100   "
        f"grade {col}{C['bold']}{s['grade']}{C['reset']}")
    log(f"  {C['dim']}{st['code_files']} files · {st['total_loc']:,} LOC · "
        f"{st['functions']} fns · {st['classes']} classes · "
        f"{st['total_nodes']} nodes / {st['total_edges']} edges{C['reset']}")
    log("")
    for key, label in METRIC_NAMES.items():
        sc = s["metrics"][key]["score"]
        c = C["green"] if sc >= 78 else C["yellow"] if sc >= 55 else C["red"]
        filled = int(round(sc / 100 * 28))
        bar = c + "█" * filled + C["dim"] + "░" * (28 - filled) + C["reset"]
        log(f"  {label:<30} {bar} {c}{sc:>5.1f}{C['reset']}")
    log("")
    notes = [(METRIC_NAMES[k], note) for k in METRIC_NAMES
             for note in s["metrics"][k]["notes"]]
    if notes:
        log(f"  {C['bold']}Top suggestions for AI-readiness:{C['reset']}")
        for label, note in notes[:5]:
            log(f"    {C['yellow']}›{C['reset']} {C['dim']}[{label}]{C['reset']} {note}")
    log("")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="peacock map",
        description="Score a repository's AI-readiness and open an interactive "
                    "3D/2D map of its files, classes, functions and calls.",
    )
    parser.add_argument("repo", nargs="?", default=".", help="repository root")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"local server port (default {DEFAULT_PORT}; "
                             "the next free port is used if it is taken)")
    parser.add_argument("--out", default=None,
                        help="report directory (default: <repo>/.peacock/report)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--build-only", action="store_true",
                        help="write the report and exit without serving it")
    parser.add_argument("--json", action="store_true",
                        help="print the scorecard as JSON and exit")
    parser.add_argument("--max-nodes", type=int, default=2500,
                        help="maximum nodes drawn on the map (default 2500)")
    parser.add_argument("--view", choices=("nebula", "atlas"), default="nebula",
                        help="initial view: 'nebula' (3D) or 'atlas' (flat 2D)")
    parser.add_argument("--all", action="store_true", help="include files ignored by Git")
    args = parser.parse_args(argv)

    data = analyze(args.repo, max_nodes=args.max_nodes, view=args.view,
                   respect_gitignore=not args.all)
    if args.json:
        print(json.dumps({k: data[k] for k in ("meta", "stats", "scores")}, indent=2))
        return 0

    print_scorecard(data)
    out_dir = args.out or os.path.join(data["meta"]["root"], ".peacock", "report")
    write_report(data, out_dir)
    log(f"{C['dim']}🦚 Report written to {out_dir}{C['reset']}")
    if args.build_only:
        return 0
    serve(out_dir, args.port, open_browser=not args.no_open,
          project_root=data["meta"]["root"])
    return 0


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    raise SystemExit(main())
