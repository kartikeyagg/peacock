"""
The five Peacock metrics.

Each returns a score in [0, 100] (higher = friendlier to a future AI agent),
plus a breakdown of sub-factors and a few human-readable notes. The metrics are
deliberately opinionated toward *agent* ergonomics — context cost, findability,
coupling — rather than classic style linting.

    1. AI Navigability          – can an agent find things fast?
    2. Context Efficiency       – how little must it load to work?
    3. Modularity & Coupling    – are boundaries clean and acyclic?
    4. Complexity & Maintain.   – how tangled is the logic?
    5. Self-Description         – do docs/types explain intent for free?
"""
from __future__ import annotations
import math
import posixpath
from collections import defaultdict


def _band(value, ideal_lo, ideal_hi, hard_lo, hard_hi):
    """Score 100 inside [ideal_lo, ideal_hi], falling linearly to 0 at the
    hard bounds. Values outside hard bounds score 0."""
    if value < hard_lo or value > hard_hi:
        return 0.0
    if ideal_lo <= value <= ideal_hi:
        return 100.0
    if value < ideal_lo:
        return 100.0 * (value - hard_lo) / (ideal_lo - hard_lo)
    return 100.0 * (hard_hi - value) / (hard_hi - ideal_hi)


def _le(value, good, bad):
    """100 when value<=good, 0 when value>=bad, linear between (lower better)."""
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (bad - value) / (bad - good)


def _ge(value, bad, good):
    """0 when value<=bad, 100 when value>=good (higher better)."""
    if value <= bad:
        return 0.0
    if value >= good:
        return 100.0
    return 100.0 * (value - bad) / (good - bad)


def _cryptic_ratio(names):
    if not names:
        return 0.0
    bad = 0
    for nm in names:
        if len(nm) <= 2 and nm not in ("id", "ok", "db", "io"):
            bad += 1
        elif nm.isupper() and len(nm) <= 3:
            bad += 1
    return bad / len(names)


def compute_metrics(files, graph, root_entries):
    code_files = [f for f in files if f.kind == "code"]
    all_symbols = [s for f in code_files for s in f.symbols]
    funcs = [s for s in all_symbols if s.kind == "function"]

    results = {}
    results["navigability"] = _navigability(code_files, all_symbols, root_entries, graph)
    results["context_efficiency"] = _context_efficiency(code_files, funcs)
    results["modularity"] = _modularity(code_files, graph)
    results["complexity"] = _complexity(funcs, code_files)
    results["self_description"] = _self_description(files, code_files, all_symbols, funcs, root_entries)

    weights = {
        "navigability": 0.22, "context_efficiency": 0.22, "modularity": 0.20,
        "complexity": 0.18, "self_description": 0.18,
    }
    overall = sum(results[k]["score"] * w for k, w in weights.items())
    return {
        "overall": round(overall, 1),
        "grade": _grade(overall),
        "metrics": results,
        "weights": weights,
    }


def _grade(s):
    return ("A+" if s >= 92 else "A" if s >= 85 else "B+" if s >= 78 else
            "B" if s >= 70 else "C+" if s >= 62 else "C" if s >= 54 else
            "D" if s >= 45 else "F")


def _mk(score, factors, notes):
    return {"score": round(max(0.0, min(100.0, score)), 1),
            "factors": {k: round(v, 1) for k, v in factors.items()},
            "notes": notes}


def _navigability(code_files, symbols, root_entries, graph):
    notes = []
    if not code_files:
        return _mk(0, {}, ["No source files found."])
    # avg file size
    avg_loc = sum(f.loc for f in code_files) / len(code_files)
    big = [f for f in code_files if f.loc > 600]
    f_size = _le(avg_loc, 200, 700)
    # naming clarity
    names = [s.name for s in symbols] + [posixpath.basename(f.path).split(".")[0] for f in code_files]
    cryptic = _cryptic_ratio(names)
    f_names = _le(cryptic, 0.05, 0.4)
    # directory shape: files per dir & depth
    per_dir = defaultdict(int)
    max_depth = 0
    for f in code_files:
        per_dir[posixpath.dirname(f.path)] += 1
        max_depth = max(max_depth, f.path.count("/"))
    avg_per_dir = sum(per_dir.values()) / max(1, len(per_dir))
    f_dir = 0.5 * _band(avg_per_dir, 2, 12, 1, 40) + 0.5 * _le(max_depth, 5, 12)
    # discoverability: README / index at root
    lower = {e.lower() for e in root_entries}
    has_readme = any(e.startswith("readme") for e in lower)
    has_index = any(e in lower for e in ("index.md", "index.js", "index.ts", "main.py",
                                         "__init__.py", "mod.rs", "lib.rs", "app.py"))
    f_entry = (60 if has_readme else 0) + (40 if has_index else 0)
    if not has_readme:
        notes.append("No README at repo root — agents lack an entry map.")
    if big:
        notes.append(f"{len(big)} file(s) over 600 LOC hurt findability (e.g. {posixpath.basename(big[0].path)}).")
    if cryptic > 0.15:
        notes.append("Many short/cryptic identifiers make symbol search unreliable.")
    score = 0.30 * f_size + 0.28 * f_names + 0.22 * f_dir + 0.20 * f_entry
    return _mk(score, {"file_size": f_size, "naming_clarity": f_names,
                       "directory_shape": f_dir, "entry_points": f_entry}, notes)


def _context_efficiency(code_files, funcs):
    notes = []
    if not code_files:
        return _mk(0, {}, ["No code."])
    # function length
    lengths = [s.length for s in funcs] or [10]
    avg_fn = sum(lengths) / len(lengths)
    long_fns = [s for s in funcs if s.length > 80]
    f_fn = _le(avg_fn, 25, 120)
    # symbols per file (cohesion) — too many = grab-bag file
    per_file = [len(f.symbols) for f in code_files if f.symbols] or [1]
    avg_sym = sum(per_file) / len(per_file)
    f_cohesion = _band(avg_sym, 1, 12, 0, 45)
    # duplication proxy: repeated non-trivial code lines
    f_dup, dup_ratio = _duplication(code_files)
    # file size (a proxy for "how much to read to grasp a unit")
    avg_loc = sum(f.loc for f in code_files) / len(code_files)
    f_load = _le(avg_loc, 150, 600)
    if long_fns:
        notes.append(f"{len(long_fns)} function(s) exceed 80 lines — costly to load whole.")
    if dup_ratio > 0.08:
        notes.append(f"~{dup_ratio*100:.0f}% duplicated code lines inflate context.")
    score = 0.34 * f_fn + 0.22 * f_cohesion + 0.22 * f_dup + 0.22 * f_load
    return _mk(score, {"function_length": f_fn, "file_cohesion": f_cohesion,
                       "duplication": f_dup, "load_size": f_load}, notes)


def _duplication(code_files):
    # We only keep FileInfo, so duplication is estimated cheaply: files sharing
    # an identical (code_lines, complexity, symbol_count) signature are a strong
    # hint of copy-paste / boilerplate that inflates an agent's context.
    sig = defaultdict(int)
    for f in code_files:
        sig[(f.code_lines, f.complexity, len(f.symbols))] += 1
    repeats = sum(c - 1 for c in sig.values() if c > 1)
    ratio = repeats / max(1, len(code_files))
    return _le(ratio, 0.02, 0.3), ratio


def _modularity(code_files, graph):
    notes = []
    file_nodes = {n["id"] for n in graph.nodes.values() if n["kind"] == "File"}
    if not file_nodes:
        return _mk(0, {}, ["No files."])
    # build file import adjacency
    adj = defaultdict(set)
    for e in graph.edges:
        if e["kind"] == "imports" and e["source"] in file_nodes and e["target"] in file_nodes:
            adj[e["source"]].add(e["target"])
    fan_out = [len(adj[f]) for f in file_nodes]
    avg_fan = sum(fan_out) / len(file_nodes)
    f_coupling = _le(avg_fan, 5, 25)
    # cycles (SCCs > 1)
    cycles = _count_cycles(adj)
    f_acyclic = _le(cycles, 0, max(3, len(file_nodes) * 0.1))
    if cycles:
        notes.append(f"{cycles} circular-import group(s) detected — hard to reason about in isolation.")
    # god files: very high fan-in
    fan_in = defaultdict(int)
    for src, dsts in adj.items():
        for d in dsts:
            fan_in[d] += 1
    hubs = [k for k, v in fan_in.items() if v > 12]
    f_hubs = _le(len(hubs), 0, max(2, len(file_nodes) * 0.08))
    if hubs:
        notes.append(f"{len(hubs)} hub file(s) that many modules depend on — change-risk hotspots.")
    # external dependency spread
    libs = [n for n in graph.nodes.values() if n["kind"] == "Library"]
    f_deps = _le(len(libs), 25, 200)
    score = 0.34 * f_coupling + 0.30 * f_acyclic + 0.20 * f_hubs + 0.16 * f_deps
    return _mk(score, {"coupling": f_coupling, "acyclicity": f_acyclic,
                       "hub_risk": f_hubs, "dependency_spread": f_deps}, notes)


def _count_cycles(adj):
    """Count strongly-connected components of size > 1 (Tarjan, iterative)."""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    counter = [0]
    cycles = [0]
    nodes = list(adj.keys())

    def strongconnect(v):
        work = [(v, iter(adj.get(v, ())))]
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                    advanced = True
                    break
                elif w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    cycles[0] += 1

    for v in nodes:
        if v not in index:
            strongconnect(v)
    return cycles[0]


def _complexity(funcs, code_files):
    notes = []
    if not funcs:
        # config/doc-only repos still get a neutral-ish score from nesting
        avg_nest = sum(f.max_nesting for f in code_files) / max(1, len(code_files))
        return _mk(_le(avg_nest, 3, 8), {"nesting": _le(avg_nest, 3, 8)},
                   ["No functions parsed; scored on nesting only."])
    cxs = [s.complexity for s in funcs]
    avg_cx = sum(cxs) / len(cxs)
    hot = [s for s in funcs if s.complexity > 15]
    f_cx = _le(avg_cx, 4, 20)
    avg_nest = sum(f.max_nesting for f in code_files) / max(1, len(code_files))
    f_nest = _le(avg_nest, 3, 9)
    p95 = sorted(cxs)[int(len(cxs) * 0.95) - 1] if len(cxs) > 5 else max(cxs)
    f_tail = _le(p95, 12, 45)
    if hot:
        notes.append(f"{len(hot)} high-complexity function(s) (>15 branches) concentrate risk.")
    if avg_nest > 5:
        notes.append("Deep average nesting — logic is hard to follow linearly.")
    score = 0.45 * f_cx + 0.25 * f_nest + 0.30 * f_tail
    return _mk(score, {"avg_complexity": f_cx, "nesting": f_nest,
                       "worst_case": f_tail}, notes)


def _self_description(files, code_files, symbols, funcs, root_entries):
    notes = []
    if not code_files:
        return _mk(0, {}, ["No code."])
    # comment density (sweet spot, not too little/much)
    tot_code = sum(f.code_lines for f in code_files) or 1
    tot_comment = sum(f.comment_lines for f in code_files)
    density = tot_comment / (tot_code + tot_comment)
    f_comments = _band(density, 0.08, 0.35, 0.0, 0.7)
    # docstring coverage
    if symbols:
        documented = sum(1 for s in symbols if s.documented) / len(symbols)
    else:
        documented = 0
    f_docs = _ge(documented, 0.05, 0.6)
    # type hints
    hinted = sum(1 for f in code_files if f.has_type_hints)
    f_types = _ge(hinted / len(code_files), 0.1, 0.7)
    # project docs (README + docs dir + markdown)
    md = [f for f in files if f.kind == "doc"]
    lower = {e.lower() for e in root_entries}
    has_readme = any(e.startswith("readme") for e in lower)
    f_project = (55 if has_readme else 0) + min(45, len(md) * 9)
    if documented < 0.2:
        notes.append("Low docstring coverage — agents must infer intent from bodies.")
    if not has_readme:
        notes.append("Add a README describing purpose, layout and entry points.")
    if hinted / len(code_files) < 0.2:
        notes.append("Sparse type annotations — signatures reveal little about data shapes.")
    score = 0.28 * f_comments + 0.30 * f_docs + 0.22 * f_types + 0.20 * f_project
    return _mk(score, {"comment_density": f_comments, "docstring_coverage": f_docs,
                       "type_hints": f_types, "project_docs": f_project}, notes)
