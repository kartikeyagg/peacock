"""
The query layer — what an agent actually calls.

Each command is a pair: a `q_*` function that returns a plain dict (so `--json`
is free) and a `r_*` function that renders that dict as compact text under a
token budget. The dispatch table at the bottom wires them together and is also
what the MCP server enumerates, so the CLI and the MCP tools can never drift
apart.

The organising principle is that **cost should scale with the relevant
subgraph, not the repo**. `who-calls` on a 700k-LOC repo costs the same as on a
5k-LOC one, because it is an index lookup on `edges(dst, kind)`, not a scan.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re

from . import fmt
from .fmt import Budget
from .index import connect, find_index, summarize, _meta_get

KIND_ALIASES = {
    "fn": "Function", "func": "Function", "function": "Function",
    "cls": "Class", "class": "Class",
    "file": "File", "dir": "Dir", "module": "Dir",
    "lib": "Library", "library": "Library",
}


class QueryError(Exception):
    pass


def open_index(path=None):
    """Open the index, walking up from cwd like git does if none is given."""
    db = path or find_index()
    if not db:
        raise QueryError(
            "no Peacock index found. Run:  peacock index <repo>")
    try:
        return connect(db)
    except (FileNotFoundError, OSError) as e:
        raise QueryError(f"cannot open index {db}: {e}. "
                         f"Run:  peacock index <repo>")


def repo_root(con):
    return _meta_get(con, "root", "")


# --------------------------------------------------------------------------- #
#  Target resolution                                                           #
# --------------------------------------------------------------------------- #
def count_matches(con, where, args, kinds=None):
    q = f"SELECT COUNT(*) FROM nodes WHERE {where}"
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args = tuple(args) + tuple(kinds)
    return con.execute(q, args).fetchone()[0]


def _like_escape(s):
    """Escape LIKE wildcards so a literal `%` or `_` matches itself."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def resolve(con, target, kinds=None, limit=25, with_total=False):
    """Turn a loose user/agent string into node rows.

    Accepts, in order of specificity: a full node id, `path:name`, an exact file
    path, a path suffix, an exact symbol name, then a substring match. Returns
    *all* matches rather than guessing — callers that need exactly one report
    the ambiguity, because silently picking the wrong `handle()` out of nine is
    the kind of error an agent cannot detect downstream.
    """
    target = (target or "").strip()
    if not target:
        raise QueryError("empty target")

    state = {"total": 0}

    def sel(where, *args):
        # The kind filter has to wrap the whole predicate. Written inline it
        # became `name LIKE ? OR path LIKE ? AND kind IN (...)`, and since AND
        # binds tighter the filter only constrained the second branch — so
        # `find engine --kind fn` returned directories.
        q = f"SELECT * FROM nodes WHERE ({where})"
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args = args + tuple(kinds)
        rows = con.execute(
            q + " ORDER BY (indeg+outdeg) DESC LIMIT ?", args + (limit,)
        ).fetchall()
        if rows:
            state["total"] = con.execute(
                q.replace("SELECT *", "SELECT COUNT(*)", 1), args).fetchone()[0]
        return rows

    def done(rows):
        return (rows, state["total"]) if with_total else rows

    # 1. exact node id
    if re.match(r"^(s:|f:|d:|lib:)", target):
        rows = sel("id = ?", target)
        if rows:
            return done(rows)

    # 2. path:name  /  path:line
    if ":" in target:
        head, _, tail = target.rpartition(":")
        if tail.isdigit():
            rows = sel("path = ? AND line = ?", head, int(tail))
            if rows:
                return done(rows)
        elif head:
            rows = sel("path = ? AND name = ?", head, tail)
            if rows:
                return done(rows)
            rows = sel("path LIKE ? ESCAPE '\\' AND name = ?",
                       "%" + _like_escape(head), tail)
            if rows:
                return done(rows)

    # 3. exact path
    rows = sel("path = ? AND kind IN ('File','Dir')", target)
    if rows:
        return done(rows)

    # 4. exact symbol name — BEFORE the path-suffix probe below.
    #
    # Ordering matters more than it looks. SQLite's LIKE is case-insensitive,
    # so a path-suffix probe for "SpringApplication" matched a directory called
    # `docs/features/springapplication` and returned that instead of the class.
    # 335 symbol names were unreachable this way, including `main` and `run`,
    # and it propagated: `impact SpringApplication` resolved to the directory
    # and reported a blast radius of zero.
    rows = sel("name = ?", target)
    if rows:
        return done(rows)

    # 5. path suffix
    rows = sel("kind IN ('File','Dir') AND path LIKE ? ESCAPE '\\'",
               "%/" + _like_escape(target))
    if rows:
        return done(rows)

    # 6. substring, last resort. `%` and `_` are escaped: unescaped, a search
    # for "%" matched all 14,230 symbols.
    esc = "%" + _like_escape(target) + "%"
    return done(sel("name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'",
                    esc, esc))


def resolve_one(con, target, kinds=None):
    """Pick a node, and report how many others it was picked from.

    The count is the *true* total, not the length of the capped candidate list.
    Reporting "+24 others" when there were 137 lets an agent believe it has
    disambiguated when it has not.
    """
    rows, total = resolve(con, target, kinds, with_total=True)
    if not rows:
        raise QueryError(f"no match for {target!r}. Try:  peacock q find {target}")
    return rows[0], rows[1:], max(0, total - 1)


def _ambiguity_note(rest, budget):
    if rest:
        budget.add(f"(+{len(rest)} other symbols share this name: "
                   + ", ".join(fmt.loc_of(r) for r in rest[:4])
                   + (" ..." if len(rest) > 4 else "") + ")")


def _kinds(spec):
    if not spec:
        return None
    out = []
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        out.append(KIND_ALIASES.get(part.lower(), part))
    return out or None


def _edge_kinds(spec, default=("calls", "imports", "contains")):
    if not spec:
        return list(default)
    return [p for p in re.split(r"[,\s]+", spec.lower()) if p]


# --------------------------------------------------------------------------- #
#  overview — the ~400-token repo map                                          #
# --------------------------------------------------------------------------- #
def q_overview(con, **kw):
    s = summarize(con)

    # Roll directories up in Python. As SQL this is a LIKE-join of every file
    # against every directory — 8.9k x 4.9k on a repo the size of spring-boot,
    # which took ten seconds. `overview` is the command an agent runs first
    # every session, so it has to be instant.
    agg = {}
    for path, loc in con.execute(
            "SELECT path, loc FROM files WHERE kind='code'"):
        d = posixpath.dirname(path)
        while True:
            key = d or "."
            cur = agg.setdefault(key, [0, 0])
            cur[0] += 1
            cur[1] += loc or 0
            if not d:
                break
            d = posixpath.dirname(d)
    # Collapse pass-through directories. `src/main/java/org/springframework/`
    # and every prefix of it hold the same 742 files, so listing all six spends
    # six lines to say one thing. If a child holds everything its parent does,
    # only the child is informative.
    children = {}
    for p in agg:
        if p != ".":
            children.setdefault(posixpath.dirname(p) or ".", []).append(p)
    # A child holding 90%+ of the parent's files makes the parent redundant;
    # exact equality is too strict to collapse `src/test/` -> `src/test/java/`
    # when one stray file sits at the higher level. The root always stays, so
    # the repo total is never lost.
    interesting = {p for p in agg
                   if p == "." or not any(
                       agg[c][0] >= 0.9 * agg[p][0]
                       for c in children.get(p, ()))}
    top_dirs = [{"path": p, "files": agg[p][0], "loc": agg[p][1]}
                for p in sorted(interesting, key=lambda p: -agg[p][1])[:12]]

    # Entry points. "Imported by nothing" alone is a bad signal — in Java every
    # test class qualifies, and a list of eight test files tells an agent
    # nothing about how the system starts. Prefer a real main(), then
    # conventionally-named launchers, and keep test trees out of it.
    imported = {r[0] for r in con.execute(
        "SELECT DISTINCT dst FROM edges WHERE kind='imports' AND dst LIKE 'f:%'")}
    has_main = {r[0] for r in con.execute(
        "SELECT DISTINCT path FROM nodes WHERE kind='Function' "
        "AND name IN ('main','Main','__main__')")}
    launcher = re.compile(
        r"(^|/)(main|index|app|cli|server|run|__main__|setup|entry)\b",
        re.I)
    is_test = re.compile(
        r"(^|/)tests?/|(^|/)spec/|(^|/)test_[^/]*$|[._-]tests?\.[a-z]+$|"
        r"(?<=[a-z])Tests?\.[A-Za-z]+$")

    # Two pools unioned: everything with a main(), plus the highest-fan-out
    # files. Ranking by fan-out first and scoring afterwards hid 254 real
    # launchers on spring-boot, because a small main() class has almost no
    # outgoing edges and never survived the cut.
    cands = {}
    for r in con.execute(
            "SELECT n.* FROM nodes n JOIN files f ON f.path = n.path "
            "WHERE n.kind='File' AND f.kind='code' "
            "ORDER BY n.outdeg DESC LIMIT 2000"):
        cands[r["id"]] = dict(r)
    if has_main:
        for i in range(0, len(has_main), 400):
            chunk = list(has_main)[i:i + 400]
            qmarks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT * FROM nodes WHERE kind='File' "
                    f"AND path IN ({qmarks})", chunk):
                cands[r["id"]] = dict(r)
    cands = list(cands.values())

    def entry_rank(e):
        p = e["path"] or ""
        score = 0
        if p in has_main:
            score += 100
        if launcher.search(posixpath.basename(p)):
            score += 40
        if e["id"] not in imported:
            score += 10
        if is_test.search(p):
            score -= 200
        return (-score, -(e["outdeg"] or 0))

    entries = [e for e in sorted(cands, key=entry_rank)
               if not is_test.search(e["path"] or "")][:8]
    for e in entries:
        e["why"] = ("main()" if e["path"] in has_main else
                    "named" if launcher.search(posixpath.basename(e["path"] or ""))
                    else "unimported")

    importer_counts = {}
    for dst, n in con.execute(
            "SELECT dst, COUNT(*) n FROM edges WHERE kind='imports' "
            "AND dst LIKE 'f:%' GROUP BY dst ORDER BY n DESC LIMIT 8"):
        importer_counts[dst] = n
    hubs = []
    if importer_counts:
        qmarks = ",".join("?" * len(importer_counts))
        for r in con.execute(f"SELECT * FROM nodes WHERE id IN ({qmarks})",
                             list(importer_counts)):
            hubs.append({**dict(r), "importers": importer_counts[r["id"]]})
        hubs.sort(key=lambda h: -h["importers"])
    libs = [dict(r) for r in con.execute("""
        SELECT name, indeg FROM nodes WHERE kind='Library'
        ORDER BY indeg DESC LIMIT 12""")]
    return {"stats": s, "top_dirs": top_dirs, "entry_points": entries,
            "hubs": hubs, "libraries": libs}


def r_overview(d, b: Budget):
    s = d["stats"]
    langs = " ".join(f"{k}:{v}" for k, v in list(s["languages"].items())[:6])
    b.add(f"REPO {s['name']}  {s['code_files']} code files  "
          f"{s['total_loc']:,} LOC  {s['functions']} fn  {s['classes']} cls")
    b.add(f"LANGS {langs}")
    b.add(f"GRAPH {s['nodes']} nodes / {s['edges']} edges  "
          f"({' '.join(f'{k}:{v}' for k, v in s['edge_kinds'].items())})")

    # State call-graph coverage up front. A dropped ambiguous call is a caller
    # that `who-calls` will never report, and an agent that does not know the
    # coverage will read "3 callers" as "exactly 3 callers". Languages that
    # dispatch on objects rather than free functions (Java, C#) lose the most.
    resolved = s["edge_kinds"].get("calls", 0)
    amb = s.get("ambiguous_calls", 0)
    # Which files a compiler parsed. Their symbol lists are exact; the rest
    # came from regexes and carry the caveats PARSER reports below.
    by_lang = {}
    for lang, fe, n in s.get("frontends", []):
        by_lang.setdefault(lang, {})[fe] = n
    compiled = []
    for lang, fes in sorted(by_lang.items()):
        real = {fe: n for fe, n in fes.items() if fe != "regex"}
        if real:
            total = sum(fes.values())
            fe, n = max(real.items(), key=lambda kv: kv[1])
            fallback = fes.get("regex", 0)
            compiled.append(f"{lang}:{fe} {n}/{total}"
                            + (f" ({fallback} regex fallback)" if fallback else ""))
    if compiled:
        b.add("PARSERS " + "  ".join(compiled) + "  -- the rest: regex")
    miss = s.get("unparsed", 0)
    if miss:
        b.add(f"PARSER {miss} declaration-shaped line(s) across "
              f"{s.get('files_with_unparsed', 0)} file(s) produced no symbol "
              f"— symbol lists in those files are incomplete")
    if resolved or amb:
        # Coverage counts call sites on both sides: of the in-repo call sites
        # the index could have bound, how many it did. (Edges against sites
        # mixed units: one edge can stand for many sites.)
        bound = s.get("bound_calls") or resolved
        total = bound + amb
        pct = 100 * bound // total if total else 100
        comp = s.get("compiler_calls", 0)
        b.add(f"CALLGRAPH {resolved} edges; {bound} call sites bound / {amb} "
              f"ambiguous ({pct}% coverage)"
              + (f"; {comp} call sites bound by a compiler, "
                 f"{s.get('external_calls', 0)} to external code" if comp else "")
              + ("  -- who-calls reports per symbol whether its answer is "
                 "COMPLETE, and lists any sites left to check" if pct < 100 else ""))
    b.add()
    b.add("TOP DIRS (by LOC)")
    for r in d["top_dirs"]:
        b.add(f"  {r['path']}/  {r['files']}f {r['loc']:,}L")
    b.add()
    b.add("LIKELY ENTRY POINTS")
    for r in d["entry_points"]:
        b.add(f"  {r['path']}  ->{r['outdeg']} [{r.get('why', '')}]")
    b.add()
    b.add("HUBS (most imported)")
    for r in d["hubs"]:
        if r["importers"]:
            b.add(f"  {r['path']}  <-{r['importers']} importers")
    b.add()
    b.add("EXTERNAL LIBS  " + " ".join(
        f"{r['name']}({r['indeg']})" for r in d["libraries"]))
    b.add()
    b.add("NEXT  peacock q outline <file> | find <name> | who-calls <sym> "
          "| subgraph <seed...>")
    b.note(hint="raise --budget for the full map")


# --------------------------------------------------------------------------- #
#  outline — read a file without reading the file                              #
# --------------------------------------------------------------------------- #
def q_outline(con, target, **kw):
    row = con.execute("SELECT * FROM files WHERE path = ?", (target,)).fetchone()
    if not row:
        node, _, _amb = resolve_one(con, target, kinds=["File"])
        row = con.execute("SELECT * FROM files WHERE path = ?",
                          (node["path"],)).fetchone()
    if not row:
        raise QueryError(f"no indexed file matching {target!r}")
    path = row["path"]
    syms = [dict(r) for r in con.execute(
        "SELECT * FROM nodes WHERE path=? AND kind IN ('Function','Class') "
        "ORDER BY line", (path,))]
    imports = [dict(r) for r in con.execute(
        "SELECT raw, resolved FROM imports WHERE path=?", (path,))]
    fid = "f:" + path
    importer_total = con.execute(
        "SELECT COUNT(*) FROM edges WHERE dst=? AND kind='imports'",
        (fid,)).fetchone()[0]
    importers = [r["path"] for r in con.execute(
        "SELECT n.path AS path FROM edges e JOIN nodes n ON n.id=e.src "
        "WHERE e.dst=? AND e.kind='imports' LIMIT 200", (fid,))]
    return {"file": dict(row), "symbols": syms, "imports": imports,
            "importers": importers, "importer_total": importer_total}


def r_outline(d, b: Budget):
    f = d["file"]
    b.add(f"FILE {f['path']}  {f['lang']}  {f['loc']}L "
          f"({f['code_lines']} code / {f['comment_lines']} comment)  "
          f"c{f['complexity']} nest{f['max_nesting']}"
          + ("  typed" if f["has_types"] else ""))
    internal = [i for i in d["imports"] if i["resolved"]]
    external = [i for i in d["imports"] if not i["resolved"]]
    # Import lists are shown by basename, not full path. On a deep Java tree one
    # `IMPORTS` line of absolute paths ran to several kilobytes and consumed the
    # whole budget before a single symbol was printed — and symbols are what
    # `outline` is for. The full paths are one `deps` call away.
    if internal:
        b.add("IMPORTS " + fmt.listing(
            [posixpath.basename(i["resolved"]) for i in internal], 12)
            + ("  (peacock q deps <file> for paths)" if internal else ""))
    if external:
        b.add("EXTERNAL " + fmt.listing([i["raw"] for i in external], 12))
    if d["importers"]:
        b.add("IMPORTED BY " + fmt.shown(min(8, len(d["importers"])),
                                         d["importer_total"], "importer")
              + " " + " ".join(posixpath.basename(p)
                               for p in d["importers"][:8])
              + ("  (peacock q dependents <file> for all)"
                 if d["importer_total"] > 8 else ""))
    miss = d["file"].get("unparsed") or 0
    b.add(f"SYMBOLS ({len(d['symbols'])})"
          + (f"  !! {miss} declaration-shaped line(s) here produced no symbol "
             f"— this list is INCOMPLETE; grep to confirm" if miss else ""))
    for s in d["symbols"]:
        mark = "C" if s["kind"] == "Class" else " "
        doc = "d" if s["documented"] else "-"
        b.add(f"  {s['line']:>5} {mark}{doc} {s['name']}  "
              f"c{s['complexity']} L{s['length']} "
              f"<-{s['indeg'] or 0} ->{s['outdeg'] or 0}")
    b.note(hint="use --budget or peacock q span <sym> for one symbol")


# --------------------------------------------------------------------------- #
#  find                                                                        #
# --------------------------------------------------------------------------- #
def q_find(con, target, kind=None, limit=200, **kw):
    kinds = _kinds(kind)
    rows, total = resolve(con, target, kinds, limit=int(limit), with_total=True)
    return {"query": target, "matches": [dict(r) for r in rows],
            "total": total, "kind": kind}


def r_find(d, b: Budget):
    b.add(f"FIND {d['query']}  "
          + fmt.shown(len(d["matches"]), d["total"], "match"))
    if not d["matches"]:
        b.add("  nothing matched. Peacock indexes files, classes and "
              "functions — not constants, fields or local variables; "
              "use grep for those.")
    for r in d["matches"]:
        b.add("  " + fmt.sym_line(r))
    b.note(hint="narrow with --kind fn|class|file")


# --------------------------------------------------------------------------- #
#  callers / callees                                                           #
# --------------------------------------------------------------------------- #
def _edge_side(con, node_id, kind, direction, limit=500):
    """Neighbours on one side of a node, plus how many there really are.

    The total is not decoration. A bare `LIMIT` here silently capped `who-calls`
    at 200 and reported "(200 callers)" for a symbol with 292 — the budget
    system never saw the dropped rows, so nothing announced the cut. Any cap
    that the truncation machinery cannot see has to carry its own count.
    """
    side, other = ("dst", "src") if direction == "in" else ("src", "dst")
    total = con.execute(
        f"SELECT COUNT(*) FROM edges WHERE {side}=? AND kind=?",
        (node_id, kind)).fetchone()[0]
    rows = [dict(r) for r in con.execute(
        f"SELECT n.*, e.conf AS conf, e.lines AS lines FROM edges e "
        f"JOIN nodes n ON n.id=e.{other} "
        f"WHERE e.{side}=? AND e.kind=? ORDER BY e.conf DESC, n.path LIMIT ?",
        (node_id, kind, limit))]
    return rows, total


def _source_lines(con):
    """path -> list of lines, read on demand from the indexed repository."""
    root = repo_root(con)
    cache = {}

    def get(path):
        if path not in cache:
            try:
                with open(os.path.join(root, path), encoding="utf-8",
                          errors="ignore") as fh:
                    cache[path] = fh.read().splitlines()
            except OSError:
                cache[path] = None
        return cache[path]
    return get


def _code_at(lines_of, path, line, width=100):
    src = lines_of(path)
    if not src or not line or line > len(src):
        return ""
    text = src[line - 1].strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def q_who_calls(con, target, **kw):
    """Callers of a symbol, at their call sites, with a completeness verdict.

    The verdict is what lets an agent stop. A caller list alone cannot say
    whether it is the whole answer, so every agent grepped to find out. The
    index can say, because it knows every call site it could not bind:

      complete     no call site with this name is left unbound, and every
                   edge was bound by a compiler or sits in the same file —
                   there is nothing further to check
      guesses      edges bound by name alone (`?`/`~`); the code at each
                   call site is shown so it can be judged in place
      unresolved   call sites with this name that no tier could bind, and
                   that may call this symbol: listed, with their code, so
                   only those lines need a look
    """
    node, rest, amb = resolve_one(con, target, kinds=["Function", "Class"])
    callers, total = _edge_side(con, node["id"], "calls", "in")
    lines_of = _source_lines(con)
    sites = []
    for r in callers:
        where = [int(x) for x in (r.get("lines") or "").split(",") if x]
        for ln in where or [r["line"]]:
            sites.append({"path": r["path"], "line": ln, "caller": r["name"],
                          "conf": r["conf"],
                          "code": _code_at(lines_of, r["path"], ln)})
    un_total = con.execute("SELECT COUNT(*) FROM unbound WHERE name=?",
                           (node["name"],)).fetchone()[0]
    unresolved = []
    for u in con.execute(
            "SELECT u.path, u.line, COALESCE(n.name, u.path) AS caller "
            "FROM unbound u LEFT JOIN nodes n ON n.id = u.src "
            "WHERE u.name=? ORDER BY u.path, u.line LIMIT 50", (node["name"],)):
        unresolved.append({"path": u["path"], "line": u["line"],
                           "caller": u["caller"],
                           "code": _code_at(lines_of, u["path"], u["line"])})
    guesses = sum(1 for r in callers if (r["conf"] or 0) < 0.95)
    complete = (un_total == 0 and guesses == 0 and len(callers) == total)
    return {"target": dict(node), "ambiguous": amb, "callers": callers,
            "total": total, "sites": sites, "unresolved": unresolved,
            "unresolved_total": un_total, "guesses": guesses,
            "complete": complete}


def r_who_calls(d, b: Budget):
    t = d["target"]
    b.add(f"WHO-CALLS {fmt.loc_of(t)} {t['name']}  "
          + fmt.shown(len(d["callers"]), d["total"], "caller"))
    # One row per call site, with the code on that line: "where is this
    # called" answered in place, so nothing needs re-reading to confirm it.
    for s in d["sites"]:
        b.add(f"  {s['path']}:{s['line']} {s['caller']}{fmt.conf_tag(s['conf'])}"
              + (f"  | {s['code']}" if s["code"] else ""))
    if d["ambiguous"]:
        b.add(f"(+{d['ambiguous']} other symbols named {t['name']} — "
              "disambiguate with path:name)")
    if d["complete"]:
        b.add(f"COMPLETE: every static call site named {t['name']} in the "
              "index is bound (by a compiler or in the same file). "
              "Nothing further to check.")
    else:
        if d["guesses"]:
            b.add(f"GUESSED: {d['guesses']} caller(s) above were matched by name "
                  "only (~ import, ? unique name); the code shown is the evidence.")
        if d["unresolved_total"]:
            b.add(f"UNRESOLVED: {d['unresolved_total']} call site(s) named "
                  f"{t['name']} could not be bound and may call this — only "
                  "these need checking:")
            for u in d["unresolved"]:
                b.add(f"  {u['path']}:{u['line']} {u['caller']}"
                      + (f"  | {u['code']}" if u["code"] else ""))
    b.note()


def q_calls(con, target, **kw):
    node, rest, amb = resolve_one(con, target, kinds=["Function", "Class"])
    callees, total = _edge_side(con, node["id"], "calls", "out")
    return {"target": dict(node), "ambiguous": amb,
            "callees": callees, "total": total}


def r_calls(d, b: Budget):
    t = d["target"]
    b.add(f"CALLS FROM {fmt.loc_of(t)} {t['name']}  "
          + fmt.shown(len(d["callees"]), d["total"], "callee"))
    for r in d["callees"]:
        b.add("  " + fmt.loc_of(r) + " " + r["name"] + fmt.conf_tag(r["conf"]))
    b.note()


# --------------------------------------------------------------------------- #
#  neighbors                                                                   #
# --------------------------------------------------------------------------- #
def _expand(con, seed_ids, depth, kinds, direction="both", cap=20000):
    """Breadth-first walk. Returns ({id: (node_row, hop)}, capped).

    Two earlier bugs here both dropped nodes in silence. The frontier was sliced
    to its first 900 entries — a SQLite parameter limit dodged by throwing away
    the tail rather than looping over it — so a wide hop lost everything past
    the 900th node and never said so. And the visited cap was low enough that
    `impact` reported the same "3999 nodes" at depth 1 and depth 2, which is a
    number an agent cannot tell is a ceiling.

    Now the frontier is chunked properly, and hitting the cap is returned as a
    fact the caller must render.
    """
    seen = {sid: 0 for sid in seed_ids}
    frontier = list(seed_ids)
    placeholders = ",".join("?" * len(kinds))
    capped = False

    for hop in range(1, depth + 1):
        if not frontier or capped:
            break
        nxt = []
        for i in range(0, len(frontier), 400):
            chunk = frontier[i:i + 400]
            qmarks = ",".join("?" * len(chunk))
            sql = []
            if direction in ("out", "both"):
                sql.append(f"SELECT dst AS other FROM edges "
                           f"WHERE src IN ({qmarks}) AND kind IN ({placeholders})")
            if direction in ("in", "both"):
                sql.append(f"SELECT src AS other FROM edges "
                           f"WHERE dst IN ({qmarks}) AND kind IN ({placeholders})")
            params = []
            for _ in sql:
                params.extend(chunk)
                params.extend(kinds)
            for row in con.execute(" UNION ".join(sql), params):
                oid = row["other"]
                if oid not in seen:
                    if len(seen) >= cap:
                        capped = True
                        break
                    seen[oid] = hop
                    nxt.append(oid)
            if capped:
                break
        frontier = nxt

    ids = list(seen)
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        qmarks = ",".join("?" * len(chunk))
        for r in con.execute(f"SELECT * FROM nodes WHERE id IN ({qmarks})",
                             chunk):
            out[r["id"]] = (dict(r), seen[r["id"]])
    return out, capped


def q_neighbors(con, target, depth=2, kinds=None, direction="both", **kw):
    node, rest, amb = resolve_one(con, target)
    ek = _edge_kinds(kinds, ("calls", "imports"))
    found, capped = _expand(con, [node["id"]], int(depth), ek, direction)
    found.pop(node["id"], None)
    items = sorted(found.values(), key=lambda t: (t[1], t[0]["path"] or ""))
    return {"target": dict(node), "depth": int(depth), "edge_kinds": ek,
            "capped": capped,
            "neighbors": [{"hop": h, **n} for n, h in items]}


def r_neighbors(d, b: Budget):
    t = d["target"]
    b.add(f"NEIGHBORS {fmt.loc_of(t)} {t['name']}  depth<={d['depth']}  "
          f"via {','.join(d['edge_kinds'])}  ({len(d['neighbors'])} nodes)"
          + fmt.cap_warning(d.get("capped")))
    hop = None
    for r in d["neighbors"]:
        if r["hop"] != hop:
            hop = r["hop"]
            b.add(f"  -- hop {hop} --")
        b.add("    " + fmt.sym_line(r, show_deg=False))
    b.note(hint="reduce --depth or set --kinds calls")


# --------------------------------------------------------------------------- #
#  deps / dependents                                                           #
# --------------------------------------------------------------------------- #
def q_deps(con, target, transitive=False, **kw):
    node, _, _amb = resolve_one(con, target, kinds=["File", "Dir"])
    if transitive:
        found, capped = _expand(con, [node["id"]], 12, ["imports"], "out")
        found.pop(node["id"], None)
        items = [{"hop": h, **n} for n, h in
                 sorted(found.values(), key=lambda t: (t[1], t[0]["path"] or ""))]
    else:
        rows, tot = _edge_side(con, node["id"], "imports", "out")
        items = [{"hop": 1, **r} for r in rows]
    return {"target": dict(node), "transitive": bool(transitive),
            "deps": items, "capped": capped if transitive else False,
            "total": len(items) if transitive else tot}


def q_dependents(con, target, transitive=False, **kw):
    node, _, _amb = resolve_one(con, target, kinds=["File", "Dir"])
    if transitive:
        found, capped = _expand(con, [node["id"]], 12, ["imports"], "in")
        found.pop(node["id"], None)
        items = [{"hop": h, **n} for n, h in
                 sorted(found.values(), key=lambda t: (t[1], t[0]["path"] or ""))]
    else:
        rows, tot = _edge_side(con, node["id"], "imports", "in")
        items = [{"hop": 1, **r} for r in rows]
    return {"target": dict(node), "transitive": bool(transitive),
            "deps": items, "capped": capped if transitive else False,
            "total": len(items) if transitive else tot}


def _r_deps(d, b, label):
    t = d["target"]
    kind = "transitive" if d["transitive"] else "direct"
    b.add(f"{label} {t['path']}  ({kind}) "
          + fmt.shown(len(d["deps"]), d.get("total", len(d["deps"])), "dep")
          + fmt.cap_warning(d.get("capped")))
    for r in d["deps"]:
        prefix = f"  [{r['hop']}] " if d["transitive"] else "  "
        b.add(prefix + (r["path"] or r["name"]))
    b.note()


def r_deps(d, b):
    _r_deps(d, b, "DEPS OF")


def r_dependents(d, b):
    _r_deps(d, b, "DEPENDENTS OF")


# --------------------------------------------------------------------------- #
#  impact — blast radius before an edit                                        #
# --------------------------------------------------------------------------- #
def q_impact(con, target, depth=3, **kw):
    node, rest, amb = resolve_one(con, target)
    found, capped = _expand(con, [node["id"]], int(depth), ["calls", "imports"], "in")
    found.pop(node["id"], None)
    by_file = {}
    for n, h in found.values():
        p = n["path"] or n["name"]
        by_file.setdefault(p, []).append({"hop": h, **n})
    for v in by_file.values():
        v.sort(key=lambda r: r["hop"])
    return {"target": dict(node), "depth": int(depth), "capped": capped,
            "reached": len(found), "files": len(by_file),
            "by_file": dict(sorted(by_file.items(),
                                   key=lambda kv: min(r["hop"] for r in kv[1])))}


def r_impact(d, b: Budget):
    t = d["target"]
    b.add(f"IMPACT {fmt.loc_of(t)} {t['name']}  depth<={d['depth']}")
    b.add(f"  changing this can reach {d['reached']} node(s) "
          f"across {d['files']} file(s)" + fmt.cap_warning(d.get("capped")))
    for path, items in d["by_file"].items():
        # Name the symbols at this file's *nearest* hop only. Listing every
        # symbol under one hop number would imply a 3-hop caller is a direct
        # one, which is exactly the false confidence this command exists to
        # prevent.
        near = min(i["hop"] for i in items)
        at_hop = sorted({i["name"] for i in items
                         if i["hop"] == near and i["kind"] in ("Function", "Class")})
        deeper = len(items) - len([i for i in items if i["hop"] == near])
        line = f"  [{near}] {path}"
        if at_hop:
            line += "  " + " ".join(at_hop[:8])
            if len(at_hop) > 8:
                line += f" +{len(at_hop) - 8}"
        if deeper:
            line += f"  (+{deeper} deeper)"
        b.add(line)
    b.note(hint="lower --depth to shrink")


# --------------------------------------------------------------------------- #
#  subgraph — the minimal relevant slice for a task                            #
# --------------------------------------------------------------------------- #
def q_subgraph(con, seeds, depth=2, kinds=None, **kw):
    if isinstance(seeds, str):
        seeds = [seeds]
    seed_nodes, unresolved = [], []
    for s in seeds:
        rows = resolve(con, s, limit=3)
        if rows:
            seed_nodes.append(dict(rows[0]))
        else:
            unresolved.append(s)
    if not seed_nodes:
        raise QueryError("none of the seeds matched: " + ", ".join(seeds))
    ek = _edge_kinds(kinds, ("calls", "imports"))
    found, capped = _expand(con, [n["id"] for n in seed_nodes], int(depth), ek, "both")
    seed_ids = {n["id"] for n in seed_nodes}

    by_file = {}
    for n, h in found.values():
        if n["kind"] in ("Dir", "Library"):
            continue
        p = n["path"] or n["name"]
        by_file.setdefault(p, {"hop": h, "symbols": []})
        by_file[p]["hop"] = min(by_file[p]["hop"], h)
        if n["kind"] in ("Function", "Class"):
            by_file[p]["symbols"].append(
                {"seed": n["id"] in seed_ids, **n})
    for v in by_file.values():
        v["symbols"].sort(key=lambda r: r["line"] or 0)
    ordered = dict(sorted(by_file.items(), key=lambda kv: (kv[1]["hop"], kv[0])))
    return {"seeds": seed_nodes, "unresolved": unresolved, "depth": int(depth),
            "edge_kinds": ek, "capped": capped,
            "nodes": len(found), "files": len(ordered),
            "by_file": ordered}


def r_subgraph(d, b: Budget):
    b.add("SUBGRAPH seeds=" + ", ".join(
        f"{fmt.loc_of(s)} {s['name']}" for s in d["seeds"])
        + f"  depth<={d['depth']}  via {','.join(d['edge_kinds'])}")
    if d["unresolved"]:
        b.add("  unresolved seeds: " + ", ".join(d["unresolved"]))
    b.add(f"  {d['nodes']} nodes across {d['files']} file(s), nearest first"
          + fmt.cap_warning(d.get("capped")))
    b.add()
    for path, info in d["by_file"].items():
        b.add(f"{path}  [hop {info['hop']}]")
        for s in info["symbols"]:
            star = "*" if s["seed"] else " "
            b.add(f"  {star}{s['line']:>5} {s['name']}  c{s['complexity']} "
                  f"L{s['length']}")
    b.note(hint="raise --budget, or lower --depth for a tighter slice")


# --------------------------------------------------------------------------- #
#  span — the exact source lines, and nothing else                             #
# --------------------------------------------------------------------------- #
def q_span(con, target, context=0, **kw):
    node, rest, amb = resolve_one(con, target, kinds=["Function", "Class"])
    root = repo_root(con)
    ap = os.path.join(root, node["path"])
    if not os.path.exists(ap):
        raise QueryError(f"source file missing on disk: {node['path']}")
    with open(ap, "rb") as fh:
        raw = fh.read()
    # The line range comes from the index; the text comes from disk. If the file
    # has changed since indexing they disagree, and the result is confidently
    # mislabelled source. We store the hash already, so check it.
    row = con.execute("SELECT hash FROM files WHERE path=?",
                      (node["path"],)).fetchone()
    stale = bool(row) and row["hash"] != hashlib.sha1(raw).hexdigest()
    lines = raw.decode("utf-8", errors="ignore").splitlines()
    start = max(1, (node["line"] or 1) - int(context))
    end = min(len(lines), (node["line"] or 1) + (node["length"] or 1) - 1
              + int(context))
    return {"target": dict(node), "ambiguous": amb, "stale": stale,
            "start": start, "end": end,
            "lines": lines[start - 1:end]}


def r_span(d, b: Budget):
    t = d["target"]
    b.add(f"SPAN {t['path']}:{d['start']}-{d['end']}  {t['name']}")
    if d.get("stale"):
        b.add("  !! STALE: this file changed since indexing; the line range may "
              "not match. Re-run `peacock index .`")
    for i, line in enumerate(d["lines"], start=d["start"]):
        b.add(f"{i:>5}| {line}")
    if d["ambiguous"]:
        b.add(f"(+{d['ambiguous']} other symbols share this name)")
    b.note(hint="raise --budget to see the whole body")


# --------------------------------------------------------------------------- #
#  hubs / cycles — structural risk                                             #
# --------------------------------------------------------------------------- #
def q_hubs(con, limit=20, **kw):
    rows = [dict(r) for r in con.execute("""
        SELECT n.*,
          (SELECT COUNT(*) FROM edges e WHERE e.dst=n.id AND e.kind='imports')
            AS importers,
          (SELECT COUNT(*) FROM edges e WHERE e.src=n.id AND e.kind='imports')
            AS imports_out
        FROM nodes n WHERE n.kind='File'
        ORDER BY (importers + imports_out) DESC LIMIT ?""", (int(limit),))]
    fns = [dict(r) for r in con.execute("""
        SELECT * FROM nodes WHERE kind='Function'
        ORDER BY indeg DESC LIMIT ?""", (int(limit),))]
    return {"files": rows, "functions": fns}


def r_hubs(d, b: Budget):
    b.add("HUB FILES (imports in + out)")
    for r in d["files"]:
        b.add(f"  {r['path']}  <-{r['importers']} ->{r['imports_out']}")
    b.add()
    b.add("MOST-CALLED FUNCTIONS")
    for r in d["functions"]:
        if r["indeg"]:
            b.add(f"  {fmt.loc_of(r)} {r['name']}  <-{r['indeg']}")
    b.note()


def q_cycles(con, **kw):
    """Import cycles among files, via iterative Tarjan (recursion would blow the
    stack on a deep repo)."""
    adj = {}
    for src, dst in con.execute(
            "SELECT src, dst FROM edges WHERE kind='imports' "
            "AND src LIKE 'f:%' AND dst LIKE 'f:%'"):
        adj.setdefault(src, []).append(dst)

    index, low, on_stack, stack, counter, sccs = {}, {}, set(), [], [0], []
    for root in list(adj):
        if root in index:
            continue
        work = [(root, iter(adj.get(root, ())))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    sccs.append(sorted(c[2:] for c in comp))
    sccs.sort(key=len, reverse=True)
    return {"cycles": sccs, "count": len(sccs)}


def r_cycles(d, b: Budget):
    if not d["count"]:
        b.add("CYCLES none — the file import graph is acyclic")
        return
    b.add(f"CYCLES {d['count']} import cycle(s)")
    for i, comp in enumerate(d["cycles"], 1):
        b.add(f"  #{i} ({len(comp)} files)")
        for p in comp:
            b.add(f"     {p}")
    b.note()


# --------------------------------------------------------------------------- #
#  Dispatch                                                                    #
# --------------------------------------------------------------------------- #
COMMANDS = {
    "overview":   (q_overview,   r_overview,   [],
                   "Repo map: size, languages, top dirs, entry points, hubs."),
    "outline":    (q_outline,    r_outline,    ["target"],
                   "A file's symbols, imports and importers — read it without reading it."),
    "find":       (q_find,       r_find,       ["target"],
                   "Locate symbols or files by name or substring."),
    "who-calls":  (q_who_calls,  r_who_calls,  ["target"],
                   "Direct callers of a function."),
    "calls":      (q_calls,      r_calls,      ["target"],
                   "Direct callees of a function."),
    "neighbors":  (q_neighbors,  r_neighbors,  ["target"],
                   "K-hop neighbourhood around a node."),
    "deps":       (q_deps,       r_deps,       ["target"],
                   "What a file imports (add --transitive for the closure)."),
    "dependents": (q_dependents, r_dependents, ["target"],
                   "What imports a file (add --transitive)."),
    "impact":     (q_impact,     r_impact,     ["target"],
                   "Blast radius: everything reachable backwards from a symbol."),
    "subgraph":   (q_subgraph,   r_subgraph,   ["seeds"],
                   "Minimal relevant slice around one or more seeds — the main "
                   "context-loading command."),
    "span":       (q_span,       r_span,       ["target"],
                   "The exact source lines of one symbol."),
    "hubs":       (q_hubs,       r_hubs,       [],
                   "Most-coupled files and most-called functions."),
    "cycles":     (q_cycles,     r_cycles,     [],
                   "Import cycles among files."),
}


def run(con, command, budget=fmt.DEFAULT_BUDGET, **kwargs):
    """Execute a command and return (data_dict, rendered_text)."""
    if "depth" in kwargs and int(kwargs["depth"]) < 0:
        raise QueryError("--depth must be >= 0")
    if int(budget) <= 0:
        raise QueryError("--budget must be > 0")
    if command not in COMMANDS:
        raise QueryError(f"unknown command {command!r}. "
                         f"Known: {', '.join(sorted(COMMANDS))}")
    qfn, rfn, _, _ = COMMANDS[command]
    data = qfn(con, **kwargs)
    b = Budget(budget)
    rfn(data, b)
    return data, b.render()
