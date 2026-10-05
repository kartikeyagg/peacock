"""
Graph construction.

Turns a list of FileInfo into a typed node/edge graph:

  node kinds : Module (directory), File, Class, Function, Library (external)
  edge kinds : contains, imports, calls

The call graph is heuristic: for each function we look at the slice of source
between its declaration and the next symbol, and link it to any *known* symbol
name referenced there. It is approximate by design — good enough to reveal
structure without a per-language grammar.
"""
from __future__ import annotations
import re
import os
import posixpath


class Graph:
    def __init__(self):
        self.nodes = {}   # id -> dict
        self.edges = []   # list of dict(source,target,kind)
        self._edge_set = set()

    def add_node(self, nid, **attrs):
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, **attrs}
        else:
            self.nodes[nid].update({k: v for k, v in attrs.items() if v is not None})
        return nid

    def add_edge(self, src, dst, kind):
        if src == dst:
            return
        key = (src, dst, kind)
        if key in self._edge_set:
            return
        if src not in self.nodes or dst not in self.nodes:
            return
        self._edge_set.add(key)
        self.edges.append({"source": src, "target": dst, "kind": kind})


_WORD = re.compile(r"[A-Za-z_$][\w$]*")


def build_graph(files, source_map, max_call_scan=4000):
    """
    files       : list[FileInfo]
    source_map  : dict[rel_path -> source text] (for call extraction)
    """
    g = Graph()

    # --- module (directory) nodes ---
    dirs = set()
    for fi in files:
        d = posixpath.dirname(fi.path)
        while True:
            dirs.add(d if d else ".")
            if not d:
                break
            d = posixpath.dirname(d)
    for d in dirs:
        label = posixpath.basename(d) or "/"
        g.add_node("mod:" + d, kind="Module", label=label, path=d)
    for d in dirs:
        parent = posixpath.dirname(d)
        if d and (parent or parent == ""):
            pkey = "mod:" + (parent if parent else ".")
            if d != ".":
                g.add_edge(pkey, "mod:" + d, "contains")

    # --- file + symbol nodes ---
    symbol_index = {}   # name -> list of function node ids
    file_of_symbol = {}
    for fi in files:
        fid = "file:" + fi.path
        g.add_node(
            fid, kind="File", label=posixpath.basename(fi.path), path=fi.path,
            language=fi.language, loc=fi.loc, complexity=fi.complexity,
            file_kind=fi.kind,
        )
        parent = posixpath.dirname(fi.path)
        g.add_edge("mod:" + (parent if parent else "."), fid, "contains")

        for s in fi.symbols:
            sid = f"sym:{fi.path}:{s.name}:{s.line}"
            kind = "Class" if s.kind == "class" else "Function"
            g.add_node(
                sid, kind=kind, label=s.name, path=fi.path, line=s.line,
                length=s.length, complexity=s.complexity, documented=s.documented,
            )
            g.add_edge(fid, sid, "contains")
            if s.kind == "function":
                symbol_index.setdefault(s.name, []).append(sid)
            file_of_symbol[sid] = fi.path

    # --- import edges (file -> file  or  file -> Library) ---
    path_lookup = _build_path_lookup(files)
    for fi in files:
        fid = "file:" + fi.path
        for imp in fi.imports:
            target = _resolve_import(imp, fi.path, path_lookup)
            if target:
                g.add_edge(fid, "file:" + target, "imports")
            else:
                lib = _library_name(imp)
                if lib:
                    lid = "lib:" + lib
                    g.add_node(lid, kind="Library", label=lib, external=True)
                    g.add_edge(fid, lid, "imports")

    # --- call edges (heuristic) ---
    # Only scan when the symbol universe is a manageable size.
    common = {n for n, v in symbol_index.items() if len(v) > 6}
    for fi in files:
        src = source_map.get(fi.path)
        if not src:
            continue
        lines = src.splitlines()
        n = len(lines)
        for idx, s in enumerate(fi.symbols):
            if s.kind != "function":
                continue
            start = s.line
            end = fi.symbols[idx + 1].line - 1 if idx + 1 < len(fi.symbols) else n
            body = "\n".join(lines[start:end]) if end > start else ""
            caller = f"sym:{fi.path}:{s.name}:{s.line}"
            called = set()
            for w in _WORD.findall(body):
                # skip self, very common names and short/ambiguous identifiers
                if len(w) < 4 or w == s.name or w in common:
                    continue
                if w in symbol_index:
                    called.add(w)
            # cap out-degree so the call graph stays legible (and cheap to draw)
            for name in list(called)[:6]:
                targets = symbol_index[name]
                same = [t for t in targets if file_of_symbol.get(t) == fi.path]
                for t in (same or targets)[:1]:
                    g.add_edge(caller, t, "calls")
        max_call_scan -= len(fi.symbols)
        if max_call_scan < 0:
            break

    _compute_degrees(g)
    return g


def _compute_degrees(g):
    for n in g.nodes.values():
        n["_in"] = 0
        n["_out"] = 0
    for e in g.edges:
        if e["target"] in g.nodes:
            g.nodes[e["target"]]["_in"] += 1
        if e["source"] in g.nodes:
            g.nodes[e["source"]]["_out"] += 1
    for n in g.nodes.values():
        deg = n["_in"] + n["_out"]
        # importance drives node size in the 3D map
        n["degree"] = deg
        base = {"Module": 3.0, "File": 2.2, "Class": 2.0,
                "Function": 1.3, "Library": 2.6}.get(n["kind"], 1.0)
        n["size"] = round(base + min(deg, 40) ** 0.5, 3)


def _build_path_lookup(files):
    lookup = {}
    for fi in files:
        p = fi.path
        lookup[p] = p
        noext = os.path.splitext(p)[0]
        lookup[noext] = p
        lookup[posixpath.basename(noext)] = p
        # module-style: a/b/c.py -> a.b.c
        lookup[noext.replace("/", ".")] = p
    return lookup


def _resolve_import(imp, from_path, lookup):
    imp = imp.strip().lstrip("./")
    from_dir = posixpath.dirname(from_path)
    candidates = [
        imp, imp.replace(".", "/"),
        posixpath.normpath(posixpath.join(from_dir, imp)) if imp else "",
        posixpath.normpath(posixpath.join(from_dir, imp.replace(".", "/"))) if imp else "",
        posixpath.basename(imp), posixpath.basename(imp).replace(".", "/"),
    ]
    for c in candidates:
        if c in lookup:
            return lookup[c]
    return None


def _library_name(imp):
    imp = imp.strip()
    if not imp:
        return None
    # take the top-level package/module token
    for sep in ("/", ".", "\\", ":"):
        if sep in imp:
            head = imp.split(sep)[0]
            if head and head not in (".", ".."):
                imp = head
                break
    imp = imp.strip("<>\"' ")
    if len(imp) > 40 or not imp:
        return None
    return imp


def from_index(con):
    """(files, graph) for the 3D report, read from the agent index.

    The report used to run its own regex-only parse and a looser call scan
    (any known name in a body, capped at six per function). Reading the index
    instead gives the map the same facts `peacock q` answers from: compiler
    front-end symbols where available, and call edges resolved by javac or
    Python scope analysis before any name matching.
    """
    from .parser import FileInfo, Symbol

    files, by_path = [], {}
    for r in con.execute("SELECT * FROM files ORDER BY path"):
        fi = FileInfo(path=r["path"], language=r["lang"], kind=r["kind"],
                      loc=r["loc"] or 0, code_lines=r["code_lines"] or 0,
                      comment_lines=r["comment_lines"] or 0,
                      blank_lines=r["blank_lines"] or 0,
                      complexity=r["complexity"] or 0,
                      max_nesting=r["max_nesting"] or 0,
                      has_type_hints=bool(r["has_types"]),
                      unparsed=r["unparsed"] or 0)
        fi.frontend = r["frontend"]
        files.append(fi)
        by_path[fi.path] = fi
    for r in con.execute("SELECT path, raw FROM imports ORDER BY rowid"):
        if r["path"] in by_path:
            by_path[r["path"]].imports.append(r["raw"])

    g = Graph()
    dirs = set()
    for fi in files:
        d = posixpath.dirname(fi.path)
        while True:
            dirs.add(d if d else ".")
            if not d:
                break
            d = posixpath.dirname(d)
    for d in dirs:
        g.add_node("mod:" + d, kind="Module", label=posixpath.basename(d) or "/", path=d)
    for d in dirs:
        if d != ".":
            parent = posixpath.dirname(d)
            g.add_edge("mod:" + (parent if parent else "."), "mod:" + d, "contains")

    for fi in files:
        fid = "file:" + fi.path
        g.add_node(fid, kind="File", label=posixpath.basename(fi.path), path=fi.path,
                   language=fi.language, loc=fi.loc, complexity=fi.complexity,
                   file_kind=fi.kind)
        parent = posixpath.dirname(fi.path)
        g.add_edge("mod:" + (parent if parent else "."), fid, "contains")

    report_id = {}                      # index id -> report node id
    for r in con.execute("SELECT * FROM nodes WHERE kind IN ('Function','Class') "
                         "ORDER BY path, line"):
        fi = by_path.get(r["path"])
        if fi is None:
            continue
        kind = "class" if r["kind"] == "Class" else "function"
        s = Symbol(r["name"], kind, r["line"] or 1, r["length"] or 1,
                   r["complexity"] or 1, bool(r["documented"]))
        s.parent = r["parent"]
        fi.symbols.append(s)
        sid = f"sym:{fi.path}:{s.name}:{s.line}"
        report_id[r["id"]] = sid
        g.add_node(sid, kind=r["kind"], label=s.name, path=fi.path, line=s.line,
                   length=s.length, complexity=s.complexity, documented=s.documented)
        g.add_edge("file:" + fi.path, sid, "contains")

    for r in con.execute("SELECT id, name FROM nodes WHERE kind='Library'"):
        g.add_node("lib:" + r["name"], kind="Library", label=r["name"], external=True)
    for src, dst, kind in con.execute(
            "SELECT src, dst, kind FROM edges WHERE kind IN ('imports','calls')"):
        if kind == "imports":
            s = "file:" + src[2:] if src.startswith("f:") else None
            d = ("file:" + dst[2:] if dst.startswith("f:")
                 else "lib:" + dst[4:] if dst.startswith("lib:") else None)
        else:
            s, d = report_id.get(src), report_id.get(dst)
        if s and d:
            g.add_edge(s, d, kind)

    _compute_degrees(g)
    return files, g
