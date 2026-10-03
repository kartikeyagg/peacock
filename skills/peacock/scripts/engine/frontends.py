"""
Compiler front-ends: real parsers for the languages that have one to hand.

The regex parser has no grammar, so every construct it does not anticipate is
either omitted or fabricated, and docs/06 records what that has cost. For two
languages a real parser costs nothing extra to ship:

  python  the stdlib `ast` module — always available, since Peacock runs on it
  java    javac's own parser (com.sun.source), via JavaFacts.java and the JDK's
          single-file source launcher — used when a JDK is on PATH

Both are *parse only*: they produce the same facts the regex parser does
(symbols with exact spans, imports, and per-symbol call sites with receivers),
and call resolution downstream is unchanged. A file either front-end cannot
parse falls back to the regex parser, and every file records which parser
produced it, so `overview` can say how much of the index is compiler-grade.

Set PEACOCK_FRONTENDS=regex to disable both, e.g. to compare the two.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess

from .parser import Symbol

JAVA_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "JavaFacts.java")
JAVA_TIMEOUT = 3600
JAVA_HEAP = os.environ.get("PEACOCK_JAVA_HEAP", "3g")


def enabled(name: str) -> bool:
    """Whether front-end `name` ("python" | "java") may be used at all."""
    want = os.environ.get("PEACOCK_FRONTENDS", "python,java").lower()
    return name in {w.strip() for w in want.split(",")}


# --------------------------------------------------------------------------- #
#  Python: stdlib ast                                                          #
# --------------------------------------------------------------------------- #
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

# A call site is (name, recv, line, target). `target` says where it binds:
#   None                     unknown — the index falls back to name matching
#   "ext"                    outside the repository; no edge
#   ("at", path, line)       an exact declaration (javac)
#   ("py", [(mod, cls, name), ...])
#                            candidates in order, from Python scope analysis:
#                            `mod` is an import string ("" = this file), `cls`
#                            a class to find `name` in (None = module level)


def _dotted(expr):
    """`a.b.c` -> "a.b.c" for a pure attribute chain over a name, else None."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return ".".join(reversed(parts))


def _submodule(mod, name):
    """`from pkg import mod` names module "pkg.mod"; `from . import mod`, ".mod"."""
    return mod + name if mod.endswith(".") else mod + "." + name


class _PyScope:
    """What names mean in one Python module, as far as syntax can tell.

    Python has no static types, so this is best-effort and says so by what it
    leaves out: a name it cannot bind produces no target, and the index falls
    back to name matching for that call exactly as before. What it does bind
    it binds from the source, not by a repo-wide name lookup:

      from x import f as g ... g()     -> f in module x (aliases survive)
      self.m() inside class C          -> C.m, then C's bases in order
      v = Foo(); v.m()                 -> Foo.m
      def h(v: Foo): v.m()             -> Foo.m
      self.store = Store(); self.store.save()  -> Store.save
    """

    def __init__(self, tree):
        self.binds = {}      # name -> ("mod", modstr) | ("from", modstr, orig)
        self.local = set()   # module-level def/class names in this file
        self.classes = {}    # class name -> (bases: [classref], attrs: {attr: classref})
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.binds[a.asname] = ("mod", a.name)
                    else:
                        top = a.name.split(".")[0]
                        self.binds.setdefault(top, ("mod", top))
            elif isinstance(node, ast.ImportFrom):
                mod = "." * (node.level or 0) + (node.module or "")
                for a in node.names:
                    if a.name != "*":
                        self.binds[a.asname or a.name] = ("from", mod, a.name)
        for node in tree.body:
            if isinstance(node, _DEFS):
                self.local.add(node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.classes.setdefault(node.name, ([], {}))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases, attrs = self.classes[node.name]
                for b in node.bases:
                    ref = self.classref(b)
                    if ref:
                        bases.append(ref)
                for fn in node.body:
                    if not isinstance(fn, _FUNCS):
                        continue
                    for st in ast.walk(fn):
                        if (isinstance(st, ast.Assign) and isinstance(st.value, ast.Call)):
                            ref = self.classref(st.value.func)
                            if not ref:
                                continue
                            for t in st.targets:
                                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                                        and t.value.id == "self"):
                                    attrs.setdefault(t.attr, ref)

    def classref(self, expr):
        """A class expression -> (mod, cls), or None."""
        if isinstance(expr, ast.Subscript):          # Foo[int]
            expr = expr.value
        if isinstance(expr, ast.Name):
            n = expr.id
            if n in self.classes:
                return ("", n)
            b = self.binds.get(n)
            if b and b[0] == "from":
                return (b[1], b[2])
            return None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            b = self.binds.get(expr.value.id)
            if b and b[0] == "mod":
                return (b[1], expr.attr)
            if b and b[0] == "from":
                return (_submodule(b[1], b[2]), expr.attr)
        return None

    def chain(self, ref, depth=0):
        """A class and, for classes in this file, its bases in order."""
        out = [ref]
        if depth < 4 and ref[0] == "" and ref[1] in self.classes:
            for b in self.classes[ref[1]][0]:
                for r in self.chain(b, depth + 1):
                    if r not in out:
                        out.append(r)
        return out

    def attr_type(self, cls, attr):
        for mod, c in self.chain(("", cls)):
            if mod == "" and c in self.classes:
                ref = self.classes[c][1].get(attr)
                if ref:
                    return ref
        return None


def _local_types(fn, scope):
    """Variable -> class, from annotations and `v = Foo(...)` in one function."""
    types = {}
    args = getattr(fn, "args", None)
    for a in (args.posonlyargs + args.args + args.kwonlyargs) if args else ():
        if a.annotation is not None:
            ref = scope.classref(a.annotation)
            if ref:
                types[a.arg] = ref

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFS):
                continue
            if isinstance(child, ast.Assign) and isinstance(child.value, ast.Call):
                ref = scope.classref(child.value.func)
                if ref:
                    for t in child.targets:
                        if isinstance(t, ast.Name):
                            types.setdefault(t.id, ref)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                ref = scope.classref(child.annotation)
                if ref:
                    types.setdefault(child.target.id, ref)
            walk(child)

    walk(fn)
    return types


def _py_target(f, scope, owner, types):
    """Where one call binds, as far as scope analysis can tell."""
    if isinstance(f, ast.Name):
        n = f.id
        if n in types:
            return None                 # calling a variable: unknown
        b = scope.binds.get(n)
        if b and b[0] == "from":
            return ("py", [(b[1], None, b[2])])
        if b and b[0] == "mod":
            return None
        if n in scope.local:
            return ("py", [("", None, n)])
        return None
    if not isinstance(f, ast.Attribute):
        return None
    m, v = f.attr, f.value
    refs = None
    if isinstance(v, ast.Name):
        n = v.id
        if n in ("self", "cls") and owner:
            refs = scope.chain(("", owner))
        elif n in types:
            refs = scope.chain(types[n])
        elif n in scope.classes:
            refs = scope.chain(("", n))
        else:
            b = scope.binds.get(n)
            if b and b[0] == "mod":
                return ("py", [(b[1], None, m)])
            if b and b[0] == "from":
                # `from pkg import mod; mod.f()` or `from mod import Cls; Cls.f()`
                return ("py", [(_submodule(b[1], b[2]), None, m), (b[1], b[2], m)])
    elif isinstance(v, ast.Attribute) and _dotted(v) and scope.binds.get(
            _dotted(v).split(".")[0], ("",))[0] == "mod":
        # `os.path.join()` — a function in a dotted module path.
        head, _, rest = _dotted(v).partition(".")
        return ("py", [(scope.binds[head][1] + "." + rest, None, m)])
    elif (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
          and v.func.id == "super" and owner):
        refs = scope.chain(("", owner))[1:]
    elif (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
          and v.value.id == "self" and owner):
        ref = scope.attr_type(owner, v.attr)
        refs = scope.chain(ref) if ref else None
    if not refs:
        return None
    return ("py", [(mod, cls, m) for mod, cls in refs])


def _py_calls(fn, scope=None, owner=None):
    """Call sites in one body (a function's, a class's or a module's), in
    source order.

    A nested function's or class's *body* is skipped: it is a symbol of its
    own and owns those calls. What this body evaluates when it runs stays
    here — a nested def's decorators and default values, a nested class's
    decorators and bases. Lambdas and comprehensions are not symbols, so
    their calls belong here too.
    """
    out = []
    types = _local_types(fn, scope) if scope else {}

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCS):
                outer_parts(child)
                continue
            if isinstance(child, ast.ClassDef):
                for part in child.decorator_list:
                    visit_one(part, applied=True)
                for part in child.bases + child.keywords:
                    visit_one(part)
                continue
            visit_one(child)

    def visit_one(node, applied=False):
        # `applied`: a bare decorator `@deco` is a call to `deco` too.
        if isinstance(node, ast.Call) or applied and isinstance(
                node, (ast.Name, ast.Attribute)):
            f = node.func if isinstance(node, ast.Call) else node
            target = _py_target(f, scope, owner, types) if scope else None
            if isinstance(f, ast.Name):
                out.append((f.id, None, f.lineno, f.col_offset, target))
            elif isinstance(f, ast.Attribute):
                # The name just before the dot, as index._receiver reads it:
                # `self.recorder.save()` -> "recorder", which the resolver
                # can match against an imported module of that name.
                v = f.value
                recv = (v.id if isinstance(v, ast.Name)
                        else v.attr if isinstance(v, ast.Attribute) else "")
                # The line of the attribute name, not of a receiver chain
                # that starts several lines up.
                line = getattr(f, "end_lineno", None) or node.lineno
                out.append((f.attr, recv, line, f.col_offset, target))
        visit(node)

    def outer_parts(fdef):
        for part in fdef.decorator_list:
            visit_one(part, applied=True)
        for part in fdef.args.defaults + [
                d for d in fdef.args.kw_defaults if d is not None]:
            visit_one(part)

    visit(ast.Module(body=fn.body, type_ignores=[]))
    out.sort(key=lambda c: (c[2], c[3]))
    return [(name, recv, line, target) for name, recv, line, _, target in out]


def parse_python(text: str):
    """(symbols, spans, calls_by_index, imports), or None if it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    try:
        scope = _PyScope(tree)
    except RecursionError:
        scope = None

    # Each definition with the class it is declared directly in, if any.
    found = []

    def collect(node, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFS):
                found.append((child, cls))
                collect(child, child.name if isinstance(child, ast.ClassDef) else None)
            else:
                collect(child, cls)

    collect(tree, None)
    found.sort(key=lambda nc: (nc[0].lineno, nc[0].col_offset))

    symbols, spans, calls = [], [], {}
    for idx, (node, cls) in enumerate(found):
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        sym = Symbol(node.name, kind, node.lineno)
        sym.documented = ast.get_docstring(node, clean=False) is not None
        sym.parent = cls
        symbols.append(sym)
        spans.append((node.lineno, max(node.lineno, node.end_lineno or node.lineno)))
        # A class owns the statements in its body (they run when the class
        # is defined): field defaults, class-level decorator calls.
        try:
            calls[idx] = _py_calls(node, scope, cls if kind == "function" else None)
        except RecursionError:
            calls[idx] = _py_calls(node)

    # Module-level code owns its calls too, under index -1: `@csrf_exempt`
    # on a top-level view, `urlpatterns = [path(...)]`. Without an owner
    # those call sites were in no graph at all, and only grep could find them.
    try:
        calls[-1] = _py_calls(tree, scope, None)
    except RecursionError:
        calls[-1] = _py_calls(tree)

    # Same strings the regex parser produced, so import resolution is
    # unchanged: `import a.b` -> "a.b", `from .x import y` -> ".x",
    # `from . import y` -> ".". Unlike the regex, `import a, b` yields both.
    imports, seen = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = ["." * (node.level or 0) + (node.module or "")]
        else:
            continue
        for m in mods:
            if m and m not in seen:
                seen.add(m)
                imports.append((node.lineno, m))
    imports.sort()
    return symbols, spans, calls, [m for _, m in imports]


# --------------------------------------------------------------------------- #
#  Java: javac, batched                                                        #
# --------------------------------------------------------------------------- #
def java_available() -> bool:
    return (enabled("java") and os.path.exists(JAVA_HELPER)
            and shutil.which("java") is not None
            and shutil.which("javac") is not None)


def _source_roots(java_files):
    """Source roots for javac's -sourcepath, from each file's package line.

    `a/src/main/java/org/x/Y.java` declaring `package org.x;` has root
    `a/src/main/java`. With every root on the source path, a batch can resolve
    a call into any Java file in the repository, not just the ones it holds.
    """
    pkg = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
    roots = set()
    for ap in java_files:
        try:
            with open(ap, "r", encoding="utf-8", errors="ignore") as fh:
                head = fh.read(8192)
        except OSError:
            continue
        m = pkg.search(head)
        d = os.path.dirname(os.path.abspath(ap))
        if m:
            parts = m.group(1).split(".")
            tail = os.sep.join(parts)
            if d.endswith(os.sep + tail) or d == tail:
                d = d[: len(d) - len(tail) - 1]
            else:
                continue            # package and directory disagree
        roots.add(d)
    return sorted(roots)


def parse_java_batch(files, log=lambda *_: None, root=None, all_java=None):
    """Parse and resolve many Java files in one JVM.

    `files` is [(rel, abs_path), ...] — the files to (re)parse. `all_java` is
    every Java file in the repository (absolute paths), used for the source
    path so calls into unchanged files still resolve.

    Returns {rel: facts} for the files javac parsed cleanly. A file missing
    from the result (parse error, JVM failure, no JDK) falls back to regex;
    nothing here is allowed to fail the index.
    """
    if not files or not java_available():
        return {}
    header = ""
    if root:
        roots = _source_roots(all_java or [ap for _, ap in files])
        header = f"#ROOT\t{os.path.abspath(root)}\n#SOURCEPATH\t{os.pathsep.join(roots)}\n"
    payload = header + "".join(f"{rel}\t{ap}\n" for rel, ap in files
                               if "\t" not in rel and "\n" not in rel and "\n" not in ap)
    # The launcher prints "Picked up JAVA_TOOL_OPTIONS" on stderr; harmless,
    # but stdout must stay pure JSON lines.
    try:
        proc = subprocess.run(["java", "-Xss8m", "-Xmx" + JAVA_HEAP,
                               "--add-exports",
                               "jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
                               JAVA_HELPER],
                              input=payload,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=JAVA_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"    javac front-end unavailable ({e.__class__.__name__}); using regex")
        return {}
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["?"]
        log(f"    javac front-end failed ({tail[0][:120]}); using regex")
    out = {}
    for line in proc.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not d.get("error") and "symbols" in d:
            out[d["path"]] = d
    return out


def java_facts_to_parse(d):
    """JavaFacts JSON -> (symbols, spans, calls_by_index, imports)."""
    symbols, spans, calls = [], [], {}
    for idx, s in enumerate(sorted(d["symbols"], key=lambda s: s["line"])):
        line = max(1, int(s["line"]))
        end = max(line, int(s["end"]))
        sym = Symbol(s["name"], s["kind"], line)
        sym.parent = s.get("parent")
        symbols.append(sym)
        spans.append((line, end))
        if s.get("calls"):
            out = []
            for c in s["calls"]:
                name, recv = c[0], c[1]
                cline = c[2] if len(c) > 2 else None
                tg = c[3] if len(c) > 3 else None
                if isinstance(tg, list):
                    tg = ("at", tg[0], int(tg[1]))
                out.append((name, recv, cline, tg))
            calls[idx] = out
    imports, seen = [], set()
    for raw in d["imports"]:
        # Match the regex parser's capture (`[\w.]+`) so resolution is
        # unchanged: "a.b.*" -> "a.b.", "static a.B.m" -> "a.B.m".
        m = raw.split("*")[0]
        if m and m not in seen:
            seen.add(m)
            imports.append(m)
    return symbols, spans, calls, imports
