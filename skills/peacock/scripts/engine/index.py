"""
The agent-facing index.

This is the half of Peacock that has nothing to do with pictures. Where
`layout.py` turns the graph into something a human can look at, this turns it
into something an agent can *query* — a SQLite database of nodes, edges and
symbol references that answers "who calls this?" in one lookup instead of a
repo-wide grep.

Design constraints, in priority order:

  1. **Stdlib only.** sqlite3 ships with Python, so the zero-install promise
     survives.
  2. **Stable IDs.** A symbol is `s:<path>#<name>`; the line number is a mutable
     *attribute*, never part of the identity. Editing line 5 of a file must not
     invalidate every symbol below it, or the index cannot serve as memory
     across sessions.
  3. **Incremental.** Parsing is the expensive part (file I/O + regex over every
     line). Edge building is pure in-memory work over cached facts. So we cache
     per-file parse results keyed by content hash, and rebuild the entire edge
     set from cache on every index run. Re-indexing a repo where one file
     changed re-parses one file.
  4. **No layout data.** Not one float of it. `x/y/z/ax/ay/size` are render
     concerns and pure overhead in an agent's context window.

Schema
------
    meta      k/v: repo root, schema version, index time, stats
    files     one row per parsed file + its content hash (the cache key)
    imports   raw import strings per file, and what they resolved to
    nodes     Dir | File | Class | Function | Library
    edges     contains | imports | calls, each with a confidence
    refs      per-symbol call-site candidates (cached so call resolution
              never needs to re-read source)
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sqlite3
import time

from . import frontends
from .languages import (lang_for, IGNORE_DIRS, CONDITIONAL_IGNORE_DIRS,
                        BUILD_MANIFESTS, VENV_DIRS, VENV_MARKERS,
                        DOC_EXTS, CONFIG_EXTS, GENERIC_BRANCH)
from .parser import parse_source, strip_noise

SCHEMA_VERSION = 9
MAX_FILE_BYTES = 1_200_000
MAX_REFS_PER_SYMBOL = 256
INDEX_DIRNAME = ".peacock"
INDEX_FILENAME = "index.db"

# Identifiers that look like calls but never are. Keeps `refs` small and the
# call graph honest — every one of these would otherwise resolve against some
# unrelated user function that happens to share the name.
STOPWORDS = frozenset("""
if else elif for while switch case catch except finally try do return yield
and or not in is new delete typeof instanceof sizeof throw throws await async
func def fun function class struct enum interface trait impl module package
import from export const let var static public private protected internal
print println printf fprintf sprintf format echo log warn error info debug
len str int float bool list dict set tuple map filter reduce range enumerate
zip sorted reversed min max sum abs round type isinstance getattr setattr
hasattr super self this null nil none true false void string number boolean
array object array_merge count push pop shift append add remove get put
""".split())

# A call site: an identifier immediately followed by `(`. Requiring the paren is
# what separates this from graph.py's bare-word scan — it drops variable names,
# type annotations and comments, which is most of the false-positive volume.
#
# Whether the name was preceded by `.`, `->` or `::` matters enormously and is
# computed separately (see _is_method_call). `s.close()` and `close()` are not
# the same claim: the first says "some object has a close method", which
# identifies nothing, while the second names a function in scope.
_CALLSITE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_MIN_REF_LEN = 2


def _receiver(body, at):
    """The receiver of a member access, or None for a bare call.

    Returns ("", ...) for `x.f()` where the receiver is not a simple name, and
    the identifier for `mod.f()`. The distinction matters: `fmt.shown()` where
    `fmt` is an imported module names exactly one target, while `x.handle()`
    names a member of an unknown type.
    """
    j = at - 1
    while j >= 0 and body[j] in " \t":
        j -= 1
    if j < 0:
        return None
    if body[j] == ".":
        op_end = j
    elif j >= 1 and body[j - 1:j + 1] in ("->", "::"):
        op_end = j - 1
    else:
        return None
    k = op_end - 1
    while k >= 0 and body[k] in " \t":
        k -= 1
    end = k + 1
    while k >= 0 and (body[k].isalnum() or body[k] in "_$"):
        k -= 1
    return body[k + 1:end] if end > k + 1 else ""


# --------------------------------------------------------------------------- #
#  Identity                                                                    #
# --------------------------------------------------------------------------- #
def dir_id(path: str) -> str:
    return "d:" + (path or ".")


def file_id(path: str) -> str:
    return "f:" + path


def lib_id(name: str) -> str:
    return "lib:" + name


def sym_id(path: str, name: str, disc: str = "") -> str:
    """Stable symbol identity. Line number is deliberately absent.

    `disc` disambiguates repeated names within one file (overloads, or a method
    name reused across two classes). It is a hash of the symbol's own body, not
    a position, because a positional index is not stable: inserting a *second*
    `process()` above the first would hand `s:t.py#process` to the new function
    and silently repoint any id an agent had remembered.

    When a name is duplicated, every occurrence carries a discriminator — the
    first one included. That way adding a third does not disturb the other two.
    """
    base = f"s:{path}#{name}"
    return f"{base}~{disc}" if disc else base


def _discriminators(symbols, bodies):
    """One discriminator per symbol: empty when the name is unique in the file.

    Falls back to a positional index only when two same-named symbols also have
    byte-identical bodies, where no content-based scheme can tell them apart.
    """
    counts = {}
    for s in symbols:
        counts[s.name] = counts.get(s.name, 0) + 1

    out, used = [], set()
    for idx, s in enumerate(symbols):
        if counts[s.name] < 2:
            out.append("")
            continue
        digest = hashlib.sha1(bodies[idx].encode("utf-8", "ignore")).hexdigest()[:6]
        key = (s.name, digest)
        if key in used:
            n = 2
            while (s.name, f"{digest}.{n}") in used:
                n += 1
            digest = f"{digest}.{n}"
            key = (s.name, digest)
        used.add(key)
        out.append(digest)
    return out


# --------------------------------------------------------------------------- #
#  Discovery                                                                   #
# --------------------------------------------------------------------------- #
def _glob_to_regex(pat):
    """Translate one gitignore glob into a regex fragment.

    `*` stops at a path separator, `**` crosses them — the distinction that
    makes `src/*.py` and `src/**/*.py` mean different things.
    """
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append(pat[i:j + 1])
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class Ignore:
    """A small .gitignore matcher.

    Generated output is the single biggest source of junk in an agent's view of
    a repo — a `report/` or `dist/` directory duplicates real source, doubles
    the symbol table, and invents import edges that no human wrote. Git already
    knows what isn't source, so we read its answer instead of guessing.

    Supports the subset that matters: comments, negation, anchoring, directory-
    only patterns, `*` vs `**`, and nested .gitignore files (rules apply from
    the directory that declares them, and the last match wins).
    """

    def __init__(self):
        self.rules = {}      # rel dir -> [(compiled, negate, dir_only)]

    def load_dir(self, rel_dir, abs_dir):
        path = os.path.join(abs_dir, ".gitignore")
        if not os.path.exists(path):
            self.rules[rel_dir] = []
            return
        rules = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except OSError:
            self.rules[rel_dir] = []
            return
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = line.startswith("/") or "/" in line
            line = line.lstrip("/")
            frag = _glob_to_regex(line)
            prefix = "" if anchored else "(?:.*/)?"
            rules.append((re.compile("^" + prefix + frag + "$"),
                          negate, dir_only))
        self.rules[rel_dir] = rules

    def ignored(self, rel_path, is_dir):
        """Last matching rule wins, walking from the root down.

        Each rule is tested against the path *and every ancestor directory of
        it*, because `build/` must exclude `build/x.py` and not merely the
        directory entry. In practice `discover` prunes ignored directories
        before descending, so this path matters mainly when a caller asks about
        a file directly.
        """
        verdict = False
        parts = rel_path.split("/")
        for depth in range(len(parts)):
            base = "/".join(parts[:depth]) or "."
            rules = self.rules.get(base)
            if not rules:
                continue
            sub_parts = parts[depth:]
            for k in range(1, len(sub_parts) + 1):
                sub = "/".join(sub_parts[:k])
                # anything with path components after it is a directory
                sub_is_dir = is_dir or k < len(sub_parts)
                for rx, negate, dir_only in rules:
                    if dir_only and not sub_is_dir:
                        continue
                    if rx.match(sub):
                        verdict = not negate
        return verdict


def discover(root, respect_gitignore=True):
    """Yield (rel_path, abs_path) for every candidate file under root.

    Symlinks that resolve to a file already yielded are skipped. Indexing one
    twice duplicates every symbol in it, which then loses the "unique name in
    repo" tier that call resolution depends on.
    """
    root = os.path.abspath(root)
    ig = Ignore() if respect_gitignore else None
    seen_real = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = "."
        if ig is not None:
            ig.load_dir(rel_dir, dirpath)

        here = set(filenames)
        beside_manifest = bool(here & BUILD_MANIFESTS)
        keep = []
        for d in dirnames:
            if d in IGNORE_DIRS or d.startswith("."):
                continue
            if d in CONDITIONAL_IGNORE_DIRS and not beside_manifest:
                # `build`, `bin`, `env` and friends are ordinary package names.
                # Only treat one as build output when a build manifest sits
                # beside it; otherwise org.springframework.boot.env disappears.
                pass
            elif d in CONDITIONAL_IGNORE_DIRS:
                continue
            elif d in VENV_DIRS:
                probe = os.path.join(dirpath, d)
                if any(os.path.exists(os.path.join(probe, m))
                       for m in VENV_MARKERS):
                    continue
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if ig is not None and ig.ignored(rel, True):
                continue
            keep.append(d)
        dirnames[:] = keep

        for fn in filenames:
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            if ig is not None and ig.ignored(rel, False):
                continue
            ap = os.path.join(dirpath, fn)
            if os.path.islink(ap):
                real = os.path.realpath(ap)
                if real in seen_real:
                    continue
                seen_real.add(real)
            else:
                seen_real.add(os.path.realpath(ap))
            yield rel, ap


def index_path_for(root, out=None):
    if out:
        return os.path.abspath(out)
    return os.path.join(os.path.abspath(root), INDEX_DIRNAME, INDEX_FILENAME)


def find_index(start=None):
    """Walk up from `start` looking for a .peacock/index.db, like git does.

    Lets an agent run `peacock q` from anywhere inside the repo without having
    to know where the root is.
    """
    cur = os.path.abspath(start or os.getcwd())
    while True:
        cand = os.path.join(cur, INDEX_DIRNAME, INDEX_FILENAME)
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


# --------------------------------------------------------------------------- #
#  Database                                                                    #
# --------------------------------------------------------------------------- #
DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, lang TEXT, kind TEXT, loc INT, code_lines INT,
    comment_lines INT, blank_lines INT, complexity INT, max_nesting INT,
    has_types INT, hash TEXT, unparsed INT DEFAULT 0,
    frontend TEXT DEFAULT 'regex'
);
CREATE TABLE IF NOT EXISTS imports (
    path TEXT, raw TEXT, resolved TEXT
);
CREATE INDEX IF NOT EXISTS imports_path ON imports(path);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT, line INT,
    length INT, complexity INT, documented INT, lang TEXT,
    indeg INT DEFAULT 0, outdeg INT DEFAULT 0, parent TEXT
);
CREATE INDEX IF NOT EXISTS nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name);
CREATE TABLE IF NOT EXISTS edges (
    src TEXT, dst TEXT, kind TEXT, conf REAL, lines TEXT,
    PRIMARY KEY (src, dst, kind)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, kind);
CREATE TABLE IF NOT EXISTS unbound (
    name TEXT, src TEXT, path TEXT, line INT
);
CREATE INDEX IF NOT EXISTS unbound_name ON unbound(name);
CREATE TABLE IF NOT EXISTS refs (
    sym TEXT, path TEXT, name TEXT, is_method INT DEFAULT 0, recv TEXT,
    line INT, target TEXT
);
CREATE INDEX IF NOT EXISTS refs_path ON refs(path);
"""


def connect(db_path, create=False):
    if not create and not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if create:
        con.executescript(DDL)
    return con


def _meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def _meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


# --------------------------------------------------------------------------- #
#  Parse stage (the expensive, cacheable half)                                 #
# --------------------------------------------------------------------------- #
def _indent(line):
    w = 0
    for ch in line:
        if ch == " ":
            w += 1
        elif ch == "\t":
            w += 4
        else:
            break
    return w


def _symbol_spans(fi, lines):
    """Where each symbol's body actually ends.

    Two wrong answers to avoid. Ending at EOF hands every trailing line of
    module-level code to the file's last function, which then appears to call
    things it does not. Ending at the *next symbol* truncates any function that
    contains a nested definition — a Python inner `def`, a Java anonymous
    class — so `span` returned a signature and called it the symbol, silently,
    at any budget.

    So the body runs while lines are blank or indented deeper than the
    declaration, and a nested symbol does not stop it. Only a line at or above
    the declaration's own indentation does.
    """
    n = len(lines)
    spans = []
    for idx, s in enumerate(fi.symbols):
        start = s.line
        decl_indent = _indent(lines[start - 1]) if start - 1 < n else 0
        # A sibling or outer symbol ends this one; a nested (deeper) symbol
        # is part of its body.
        hard_end = n
        for nxt in fi.symbols[idx + 1:]:
            if nxt.line - 1 < n and _indent(lines[nxt.line - 1]) <= decl_indent:
                hard_end = nxt.line - 1
                break
        end = start
        for i in range(start, min(hard_end, n)):
            line = lines[i]
            if not line.strip():
                continue
            if _indent(line) <= decl_indent:
                break
            end = i + 1
        spans.append((start, max(end, start)))
    return spans


def _filter_refs(own_name, calls):
    """Apply the regex harvest's call-site filters to a front-end's calls, so
    the resolver sees one kind of row.

    A call the compiler bound to a target is kept whatever its name: `get` is
    a stopword only because *guessing* its target is hopeless, and a
    recursive call is a real edge once it is known to be one.
    """
    names = []
    for w, recv, line, target in calls:
        if target is None and (len(w) < _MIN_REF_LEN or w == own_name
                               or w in STOPWORDS):
            continue
        names.append((w, recv, line, target))
        if len(names) >= MAX_REFS_PER_SYMBOL:
            break
    return names


def _encode_target(t):
    """Call target -> the TEXT stored in refs.target (None stays NULL)."""
    if t is None or t == "ext":
        return t
    if t[0] == "at":
        return f"at\t{t[1]}\t{t[2]}"
    return "py\t" + json.dumps(t[1])


def _documented(lines, start, line_comments):
    """A comment directly above the declaration, looking past annotations."""
    j = start - 2
    while j >= 0 and lines[j].strip().startswith("@"):
        j -= 1
    if j < 0:
        return False
    prev = lines[j].strip()
    return prev.endswith("*/") or any(prev.startswith(lc) for lc in line_comments)


def _apply_frontend(fi, parsed, lines, clean, frontend):
    """Replace the regex parser's symbols, imports and refs with a compiler's.

    Line metrics (LOC, comments, file complexity, type hints) stay the regex
    parser's: they are counts over text, which it gets right. Returns
    (refs, spans).
    """
    symbols, spans, calls, imports = parsed
    _, defn = lang_for(fi.path)
    line_comments = defn["line_comment"] if defn else []
    for s, (a, b) in zip(symbols, spans):
        s.length = b - a + 1
        s.complexity = 1 + len(GENERIC_BRANCH.findall("\n".join(clean[a - 1:b])))
        if not s.documented:
            s.documented = _documented(lines, a, line_comments)
    fi.symbols = symbols
    fi.imports = imports
    fi.unparsed = 0          # a real parser saw every declaration
    fi.frontend = frontend
    refs = {}
    for idx, found in calls.items():
        # idx -1 is module-level code, owned by the file itself.
        kept = _filter_refs(symbols[idx].name if idx >= 0 else "", found)
        if kept:
            refs[idx] = kept
    return refs, spans


def _extract_refs(fi, lines, spans):
    """Per-symbol call sites from the regex harvest, each with its line.

    Returns {symbol_index: [(name, recv, line, None), ...]}. The target is
    always unknown here; only a compiler front-end can supply one. This is
    the only place we touch source text during indexing, which is why the
    result is cached.
    """
    out = {}
    for idx, s in enumerate(fi.symbols):
        if s.kind != "function":
            continue
        start, end = spans[idx]
        if end <= start:
            continue
        body = "\n".join(lines[start:end])
        names = []
        for m in _CALLSITE.finditer(body):
            w = m.group(1)
            if len(w) < _MIN_REF_LEN or w == s.name or w in STOPWORDS:
                continue
            line = start + 1 + body.count("\n", 0, m.start())
            names.append((w, _receiver(body, m.start()), line, None))
            if len(names) >= MAX_REFS_PER_SYMBOL:
                break
        if names:
            out[idx] = names
    return out


def _parse_one(rel, ap, java_facts=None):
    """Parse a single file into (FileInfo, refs, spans, discs, sha1).

    Python goes through the stdlib `ast`; Java through javac when the caller
    has batch-parsed it (`java_facts`). Anything else, or a file the compiler
    rejects, goes through the regex parser.
    """
    name, defn = lang_for(rel)
    ext = os.path.splitext(rel)[1].lower()
    if defn is None and ext not in DOC_EXTS and ext not in CONFIG_EXTS:
        return None
    try:
        if os.path.getsize(ap) > MAX_FILE_BYTES:
            return None
        with open(ap, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    digest = hashlib.sha1(raw).hexdigest()
    text = raw.decode("utf-8", errors="ignore")
    fi = parse_source(rel, text)
    if fi is None:
        return None
    fi.frontend = "regex"
    parsed = frontend = None
    if fi.kind == "code" and fi.language == "python" and frontends.enabled("python"):
        parsed, frontend = frontends.parse_python(text), "ast"
    elif fi.kind == "code" and fi.language == "java" and java_facts:
        parsed, frontend = frontends.java_facts_to_parse(java_facts), "javac"
    if fi.kind != "code" or (not fi.symbols and not (parsed and (parsed[0] or parsed[2]))):
        if parsed is not None:
            fi.symbols, fi.imports, fi.unparsed = [], parsed[3], 0
            fi.frontend = frontend
        return fi, {}, [], [], digest
    lines = text.splitlines()
    # Call sites are harvested from source with comments and string literals
    # blanked out; otherwise a `@see Foo#refresh()` in a doc comment becomes a
    # call edge, at the highest confidence tier.
    clean = strip_noise(text, fi.language).splitlines()
    while len(clean) < len(lines):
        clean.append("")
    if parsed is not None:
        refs, spans = _apply_frontend(fi, parsed, lines, clean, frontend)
    else:
        spans = _symbol_spans(fi, lines)
        refs = _extract_refs(fi, clean, spans)
    bodies = ["\n".join(clean[a - 1:b]) for a, b in spans]
    discs = _discriminators(fi.symbols, bodies)
    return fi, refs, spans, discs, digest


def _digest(ap):
    try:
        with open(ap, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None


def _store_file(con, fi, refs, spans, discs, digest):
    """Replace every cached row for one file."""
    _forget_file(con, fi.path)
    con.execute(
        "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fi.path, fi.language, fi.kind, fi.loc, fi.code_lines, fi.comment_lines,
         fi.blank_lines, fi.complexity, fi.max_nesting, int(fi.has_type_hints),
         digest, int(getattr(fi, "unparsed", 0)),
         getattr(fi, "frontend", "regex")))
    con.executemany("INSERT INTO imports(path, raw, resolved) VALUES (?,?,NULL)",
                    [(fi.path, imp) for imp in fi.imports])

    rows, refrows = [], []
    for idx, s in enumerate(fi.symbols):
        sid = sym_id(fi.path, s.name, discs[idx])
        kind = "Class" if s.kind == "class" else "Function"
        start, end = spans[idx]
        rows.append((sid, kind, s.name, fi.path, s.line, max(1, end - start + 1),
                     s.complexity, int(s.documented), fi.language,
                     getattr(s, "parent", None)))
        for nm, recv, line, target in refs.get(idx, ()):
            refrows.append((sid, fi.path, nm, int(recv is not None), recv, line,
                            _encode_target(target)))
    # Module-level calls belong to the file node: `who-calls` then reports
    # `urls.py:42 urls.py` for a decorator on a top-level view.
    for nm, recv, line, target in refs.get(-1, ()):
        refrows.append((file_id(fi.path), fi.path, nm, int(recv is not None), recv,
                        line, _encode_target(target)))
    con.executemany(
        "INSERT OR REPLACE INTO nodes(id,kind,name,path,line,length,complexity,"
        "documented,lang,parent) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO refs(sym,path,name,is_method,recv,line,target) "
        "VALUES (?,?,?,?,?,?,?)", refrows)


def _forget_file(con, path):
    con.execute("DELETE FROM files WHERE path=?", (path,))
    con.execute("DELETE FROM imports WHERE path=?", (path,))
    con.execute("DELETE FROM refs WHERE path=?", (path,))
    con.execute("DELETE FROM nodes WHERE path=? AND kind IN "
                "('Function','Class')", (path,))


# --------------------------------------------------------------------------- #
#  Resolution stage (cheap, always re-run in full)                             #
# --------------------------------------------------------------------------- #
def _build_path_lookup(paths, lang_of=None):
    """Two maps, deliberately kept apart.

    `exact` holds full and extension-stripped paths, which identify a file
    unambiguously. `basename` holds the last component, which very often does
    not — a repo with `engine/parser.py` and `lib/parser.rb` has two files
    called "parser", and resolving `.parser` to whichever happened to be
    inserted first invents an edge between unrelated languages. So basenames
    map to a *list*, and are only usable when that list has one entry.
    """
    exact, basename, suffix = {}, {}, {}
    for p in paths:
        noext = os.path.splitext(p)[0]
        exact[p] = p
        exact.setdefault(noext, p)
        exact.setdefault(noext.replace("/", "."), p)
        basename.setdefault(posixpath.basename(noext), []).append(p)
        # Every trailing path fragment, so a package-qualified import can be
        # matched by its whole path even when the source root is buried under
        # something like src/main/java/.
        parts = noext.split("/")
        for i in range(len(parts)):
            suffix.setdefault("/".join(parts[i:]), []).append(p)

    # A package import names a directory, not a file. `from engine import fmt`
    # has to land on `engine/__init__.py` or it lands nowhere and gets filed as
    # an external library — which is how this repo ended up not knowing that
    # peacock.py imports its own engine package.
    for p in paths:
        base = posixpath.basename(p)
        if base in ("__init__.py", "index.js", "index.ts", "index.jsx",
                    "index.tsx", "mod.rs", "__init__.pyi"):
            pkg = posixpath.dirname(p)
            if pkg:
                exact.setdefault(pkg, p)
                exact.setdefault(pkg.replace("/", "."), p)
                basename.setdefault(posixpath.basename(pkg), []).append(p)
    return exact, basename, suffix


def _resolve_import(imp, from_path, lookup, lang_of=None):
    """Resolve an import to a repo file, nearest scope first.

    Order matters: directory-relative before repo-root before bare basename.
    `from .parser import x` inside `engine/` must find `engine/parser.py`, and
    only a globally unique basename may be trusted as a fallback.

    Leading dots carry level information — `..parser` means the *parent*
    package — so they are counted rather than stripped.

    The basename fallback additionally refuses to cross languages. Go's
    `import "fmt"` was resolving to a Python `engine/fmt.py`, which is not a
    near miss but a category error: it invents a dependency between files that
    cannot reference each other, and then reports it as a direct import.
    """
    exact, basename, suffix = lookup
    imp = (imp or "").strip()
    if not imp:
        return None

    level = len(imp) - len(imp.lstrip("."))
    imp = imp.lstrip(".")
    if not imp:
        return None
    dotted = imp.replace(".", "/")

    base_dir = posixpath.dirname(from_path)
    for _ in range(max(0, level - 1)):
        base_dir = posixpath.dirname(base_dir)

    def ok(target):
        return (target and target != from_path
                and _lang_compatible(lang_of, from_path, target))

    candidates = []
    if level and base_dir is not None:
        candidates += [posixpath.normpath(posixpath.join(base_dir, dotted))]
    elif base_dir:
        candidates += [posixpath.normpath(posixpath.join(base_dir, imp)),
                       posixpath.normpath(posixpath.join(base_dir, dotted))]
    if not level:
        candidates += [imp, dotted]
    for c in candidates:
        t = exact.get(c)
        if ok(t):
            return t
    if level:
        return None

    # A package-qualified import names a full path, and matching only its last
    # segment throws that information away. `org.apache.commons.logging.Log`
    # would find any lone `Log.java` in the repo — 12.7% of Java import edges
    # were fabricated this way, and they poison deps, dependents, hubs and
    # every importer count. If the name carries a package path, that path has
    # to match; only a bare single-segment name may fall back to a basename.
    if "." in imp:
        hits = [h for h in (suffix.get(dotted) or []) if ok(h)]
        return hits[0] if len(hits) == 1 else None

    hits = [h for h in (basename.get(posixpath.basename(dotted)) or [])
            if ok(h)]
    return hits[0] if len(hits) == 1 else None


# Languages that legitimately import each other's files. Everything else must
# match exactly: the extension-stripped lookup makes Go's `import "fmt"` and a
# Python `fmt.py` collide on the key "fmt", and resolving that is not a near
# miss but a category error — it invents a dependency between two files that
# cannot reference each other at all.
_LANG_FAMILY = {
    "c": "c", "cpp": "c",
    "javascript": "js", "typescript": "js",
    "java": "jvm", "kotlin": "jvm", "scala": "jvm", "groovy": "jvm",
}


def _lang_compatible(lang_of, a, b):
    if lang_of is None:
        return True
    la, lb = lang_of.get(a), lang_of.get(b)
    if la is None or lb is None or la == lb:
        return True
    return (_LANG_FAMILY.get(la) is not None
            and _LANG_FAMILY.get(la) == _LANG_FAMILY.get(lb))


# A library name has to look like an identifier or a package path. Without this
# the parser's string-blind regexes leak CSS colours (`#66b8ff`) and keywords
# lifted out of docstrings into the dependency list.
_LIB_OK = re.compile(r"^@?[A-Za-z_][\w.\-]*$")
_LIB_REJECT = frozenset(
    "from import require include use package module export default as".split())


# Reverse-DNS package roots. Truncating `org.springframework.boot.X` at the
# first segment yields "org", which names nothing — on spring-boot the library
# list read `org(5608) java(3525) io(489)`. These roots take two segments.
_DNS_ROOTS = frozenset(
    "org com io net javax jakarta edu gov uk co dev cloud app me".split())


def _library_name(imp):
    imp = (imp or "").strip().strip("<>\"' ")
    if not imp:
        return None
    if "/" in imp or "\\" in imp:
        head = re.split(r"[/\\]", imp)[0]
        imp = head if head not in (".", "..") else imp
    elif "." in imp:
        parts = [p for p in imp.split(".") if p]
        if not parts:
            return None
        imp = ".".join(parts[:2]) if parts[0] in _DNS_ROOTS and len(parts) > 1 \
            else parts[0]
    elif ":" in imp:
        imp = imp.split(":")[0]
    imp = imp.strip("<>\"' ")
    if not imp or len(imp) > 40:
        return None
    if imp.split(".")[0] in _LIB_REJECT:
        return None
    if not all(_LIB_OK.match(p) for p in imp.split(".")):
        return None
    return imp


def rebuild_edges(con, log=lambda *_: None):
    """Rebuild the whole edge set from cached per-file facts.

    Pure in-memory work — no file is opened here. That is what makes an
    incremental re-index cheap: we re-parse only what changed, then redo all of
    this, which costs a fraction of a second even on a large repo.
    """
    con.execute("DELETE FROM edges")
    con.execute("DELETE FROM nodes WHERE kind IN ('Dir','File','Library')")

    frows = con.execute("SELECT * FROM files").fetchall()
    paths = [r["path"] for r in frows]
    edges = []

    # --- directory tree -----------------------------------------------------
    dirs = set()
    for p in paths:
        d = posixpath.dirname(p)
        while True:
            dirs.add(d or ".")
            if not d:
                break
            d = posixpath.dirname(d)
    dir_rows = [(dir_id(d), "Dir", posixpath.basename(d) or "/", d, None, None,
                 None, None, None) for d in dirs]
    for d in dirs:
        if d == ".":
            continue
        parent = posixpath.dirname(d) or "."
        edges.append((dir_id(parent), dir_id(d), "contains", 1.0))

    # --- files --------------------------------------------------------------
    file_rows = []
    for r in frows:
        p = r["path"]
        file_rows.append((file_id(p), "File", posixpath.basename(p), p, None,
                          r["loc"], r["complexity"], None, r["lang"]))
        edges.append((dir_id(posixpath.dirname(p) or "."), file_id(p),
                      "contains", 1.0))

    con.executemany(
        "INSERT OR REPLACE INTO nodes(id,kind,name,path,line,length,complexity,"
        "documented,lang) VALUES (?,?,?,?,?,?,?,?,?)", dir_rows + file_rows)

    # --- file contains symbol ----------------------------------------------
    for sid, spath in con.execute(
            "SELECT id, path FROM nodes WHERE kind IN ('Function','Class')"):
        edges.append((file_id(spath), sid, "contains", 1.0))

    # --- imports ------------------------------------------------------------
    lang_of = {r["path"]: r["lang"] for r in frows}
    lookup = _build_path_lookup(paths)
    libs = set()
    resolved_updates = []
    imports_of = {}          # path -> set(target path)
    for path, raw in con.execute("SELECT path, raw FROM imports"):
        target = _resolve_import(raw, path, lookup, lang_of)
        if target:
            edges.append((file_id(path), file_id(target), "imports", 1.0))
            imports_of.setdefault(path, set()).add(target)
            resolved_updates.append((target, path, raw))
        else:
            lib = _library_name(raw)
            if lib:
                libs.add(lib)
                edges.append((file_id(path), lib_id(lib), "imports", 1.0))
                resolved_updates.append((None, path, raw))
    con.executemany("UPDATE imports SET resolved=? WHERE path=? AND raw=?",
                    resolved_updates)
    if libs:
        con.executemany(
            "INSERT OR REPLACE INTO nodes(id,kind,name,path,lang) "
            "VALUES (?,?,?,NULL,NULL)",
            [(lib_id(l), "Library", l) for l in libs])

    # --- calls --------------------------------------------------------------
    edges = [e + (None,) for e in edges]
    edges.extend(_resolve_calls(con, imports_of, log, lookup, lang_of))

    con.executemany(
        "INSERT OR REPLACE INTO edges(src,dst,kind,conf,lines) VALUES (?,?,?,?,?)",
        edges)
    _compute_degrees(con)
    return len(edges)


def _resolve_calls(con, imports_of, log, lookup=None, lang_of=None):
    """Turn cached call sites into edges, most-certain evidence first.

    A call site may carry a target from a compiler front-end, and that wins:

      1.00  compiler        javac bound the call to this exact declaration
      0.97  scope           Python scope analysis bound it: an import, an
                            alias, `self.m()` in a known class, a typed local
      --    external        the front-end says it binds outside the repo:
                            no edge, and no guess (counted in meta)

    A call site without a target falls back to the name-matching tiers, which
    decide both *which* target wins and the confidence stamped on the edge:

      0.95  same file          — the definition is right there
      0.80  imported file      — this file actually imports the definer
      0.60  unique in repo     — exactly one definition exists anywhere
      --    ambiguous          — dropped, and counted in meta

    The old heuristic did none of this: it kept a single global name bag, threw
    away any name defined in more than six files, and picked an arbitrary match
    otherwise. That produced confident wrong answers, which for an agent is
    worse than no answer.

    Every call site keeps its line, and an edge carries the lines of all the
    call sites behind it, so `who-calls` can answer "where", not just "who".
    """
    # Classes belong here too: `Widget()` is a call site, and leaving classes
    # out meant asking who constructs a class always returned a confident zero.
    by_name, by_loc, by_file_name = {}, {}, {}
    for sid, name, path, line, parent in con.execute(
            "SELECT id, name, path, line, parent FROM nodes "
            "WHERE kind IN ('Function','Class')"):
        by_name.setdefault(name, []).append((sid, path))
        by_loc.setdefault((path, line), []).append((sid, name))
        by_file_name.setdefault((path, name), []).append((sid, parent, line))

    # Top-level names that could be a repo module. An import whose first
    # segment matches none of them is a library, so a call into it is
    # external rather than unknown.
    repo_heads = set()
    for (path, _name) in by_file_name:
        for part in path.split("/"):
            repo_heads.add(os.path.splitext(part)[0])

    mod_cache = {}

    def module_file(mod, path):
        if mod == "":
            return path
        key = (mod, posixpath.dirname(path) if mod.startswith(".") else "")
        if key not in mod_cache:
            mod_cache[key] = (_resolve_import(mod, path, lookup, lang_of)
                              if lookup else None)
        return mod_cache[key]

    def at_target(tpath, tline, name):
        hits = by_loc.get((tpath, tline), [])
        if len(hits) == 1:
            return hits[0][0]
        named = [sid for sid, n in hits if n == name]
        if len(named) == 1:
            return named[0]
        same = by_file_name.get((tpath, name), [])
        if len(same) == 1:
            return same[0][0]
        if same:
            return min(same, key=lambda c: abs(c[2] - tline))[0]
        return None

    def py_target(cands, path):
        """First candidate found in the repo -> sid, "ext", or None."""
        maybe_repo = False
        for mod, cls, name in cands:
            f = module_file(mod, path)
            if not f:
                head = mod.lstrip(".").split(".")[0]
                if mod.startswith(".") or head in repo_heads:
                    maybe_repo = True
                continue
            maybe_repo = True
            nodes = by_file_name.get((f, name), [])
            if cls is not None:
                nodes = [n for n in nodes if n[1] == cls]
            else:
                top = [n for n in nodes if n[1] is None]
                nodes = top or nodes
            if nodes:
                return nodes[0][0]
        return None if maybe_repo else "ext"

    # Which import names a file brought into scope, so `fmt.shown()` can be
    # told apart from `x.handle()`. The first names one module; the second
    # names a member of an unknown type.
    module_of = {}
    for p_, raw, resolved in con.execute(
            "SELECT path, raw, resolved FROM imports WHERE resolved IS NOT NULL"):
        alias = posixpath.basename(os.path.splitext(resolved)[0])
        module_of.setdefault(p_, {})[alias] = resolved
        tail = re.split(r"[./\\]", (raw or "").strip().strip("."))[-1]
        if tail:
            module_of[p_][tail] = resolved

    def guess(sym, path, name, is_method, recv):
        """Name matching: (target, conf), or "amb" / "unknown"."""
        cands = by_name.get(name)
        if not cands:
            return "unknown"
        # A module-qualified call is not a guess. `fmt.shown()` where this file
        # imports engine/fmt.py identifies exactly one definition.
        if recv:
            target_file = (module_of.get(path) or {}).get(recv)
            if target_file:
                scoped = [c for c in cands if c[1] == target_file]
                if len(scoped) == 1:
                    return scoped[0][0], 0.90
        if is_method:
            # `x.handle()` names a member, not a function in scope. It
            # identifies a unique target only when the repo has exactly one
            # symbol by that name; anything else is a coin flip.
            return (cands[0][0], 0.60) if len(cands) == 1 else "amb"
        local = [c for c in cands if c[1] == path]
        if len(local) == 1:
            return local[0][0], 0.95
        if len(local) > 1:
            return "amb"
        visible = imports_of.get(path)
        imported = [c for c in cands if visible and c[1] in visible]
        if len(imported) == 1:
            return imported[0][0], 0.80
        if imported:
            return "amb"
        if len(cands) == 1:
            return cands[0][0], 0.60
        return "amb"

    edges = {}          # (src, dst) -> [conf, set(lines)]
    # Call sites no tier could bind, kept by name: `who-calls X` lists the
    # ones named X instead of leaving the agent to grep for them.
    unbound = []
    ambiguous = unresolved = external = compiled = bound = 0
    for sym, path, name, is_method, recv, line, target in con.execute(
            "SELECT sym, path, name, is_method, recv, line, target FROM refs"):
        dst = conf = None
        if target == "ext":
            external += 1
            continue
        if target and target.startswith("at\t"):
            _, tpath, tline = target.split("\t")
            dst = at_target(tpath, int(tline), name)
            conf = 1.0
        elif target and target.startswith("py\t"):
            found = py_target(json.loads(target[3:]), path)
            if found == "ext":
                external += 1
                continue
            dst, conf = found, 0.97
        if dst is not None:
            compiled += 1
        else:
            g = guess(sym, path, name, is_method, recv)
            if g == "unknown":
                # Defined nowhere in the repo — a library or builtin call.
                unresolved += 1
                continue
            if g == "amb":
                ambiguous += 1
                unbound.append((name, sym, path, line))
                continue
            dst, conf = g
        bound += 1
        if dst == sym:
            continue
        e = edges.setdefault((sym, dst), [conf, set()])
        e[0] = max(e[0], conf)
        if line:
            e[1].add(int(line))

    con.execute("DELETE FROM unbound")
    con.executemany("INSERT INTO unbound(name, src, path, line) VALUES (?,?,?,?)",
                    unbound)
    out = [(src, dst, "calls", conf,
            ",".join(str(n) for n in sorted(lines)) or None)
           for (src, dst), (conf, lines) in edges.items()]
    _meta_set(con, "ambiguous_calls", ambiguous)
    _meta_set(con, "unresolved_calls", unresolved)
    _meta_set(con, "external_calls", external)
    _meta_set(con, "compiler_calls", compiled)
    _meta_set(con, "bound_calls", bound)
    log(f"    call edges: {len(out)} resolved ({compiled} call sites bound by "
        f"a compiler front-end), {ambiguous} ambiguous, "
        f"{unresolved + external} external/unknown")
    return out


def _compute_degrees(con):
    """Degrees count coupling only — `contains` is excluded.

    Structural containment is not a relationship an agent is asking about, and
    including it made every symbol show `<-1` minimum (its own file contains
    it). A reader seeing `<-1` naturally reads "one caller"; it meant zero.
    """
    con.execute("UPDATE nodes SET indeg=0, outdeg=0")
    con.execute("""
        UPDATE nodes SET outdeg = COALESCE(
            (SELECT COUNT(*) FROM edges WHERE edges.src = nodes.id
             AND edges.kind <> 'contains'), 0)""")
    con.execute("""
        UPDATE nodes SET indeg = COALESCE(
            (SELECT COUNT(*) FROM edges WHERE edges.dst = nodes.id
             AND edges.kind <> 'contains'), 0)""")


# --------------------------------------------------------------------------- #
#  Entry point                                                                 #
# --------------------------------------------------------------------------- #
def build_index(root, out=None, force=False, respect_gitignore=True,
                log=lambda *_: None):
    """Create or incrementally update the index for `root`. Returns a summary."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    db_path = index_path_for(root, out)
    t0 = time.time()

    fresh = force or not os.path.exists(db_path)
    if force and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
    con = connect(db_path, create=True)

    if not fresh and _meta_get(con, "schema") != str(SCHEMA_VERSION):
        log("    schema changed — rebuilding from scratch")
        con.close()
        return build_index(root, out, force=True,
                           respect_gitignore=respect_gitignore, log=log)

    known = {r["path"]: r["hash"]
             for r in con.execute("SELECT path, hash FROM files")}

    files = [(rel, ap) for rel, ap in discover(root, respect_gitignore)
             if not rel.startswith(INDEX_DIRNAME + "/")]

    # javac runs once over every changed .java file, before the main loop: a
    # JVM per file would cost more than all the parsing put together.
    java_facts = {}
    if frontends.java_available():
        changed = [(rel, ap) for rel, ap in files
                   if lang_for(rel)[0] == "java"
                   and known.get(rel) != _digest(ap)]
        if changed:
            all_java = [ap for rel, ap in files if lang_for(rel)[0] == "java"]
            java_facts = frontends.parse_java_batch(changed, log, root, all_java)
            log(f"    javac parsed {len(java_facts)}/{len(changed)} Java file(s)")

    parsed = skipped = 0
    present = set()
    with con:
        for rel, ap in files:
            res = _parse_one(rel, ap, java_facts.get(rel))
            if res is None:
                continue
            fi, refs, spans, discs, digest = res
            present.add(rel)
            if known.get(rel) == digest:
                skipped += 1
                continue
            _store_file(con, fi, refs, spans, discs, digest)
            parsed += 1

        removed = [p for p in known if p not in present]
        for p in removed:
            _forget_file(con, p)

    if not present:
        con.close()
        raise ValueError("no analyzable source files found under " + root)

    log(f"    parsed {parsed} file(s), reused {skipped} cached, "
        f"dropped {len(removed)}")

    with con:
        n_edges = rebuild_edges(con, log)
        _meta_set(con, "schema", SCHEMA_VERSION)
        _meta_set(con, "root", root)
        _meta_set(con, "name", os.path.basename(root.rstrip("/")) or "repository")
        _meta_set(con, "indexed_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        _meta_set(con, "tool", "Peacock")

    # Rebuilding every edge each run leaves a lot of free pages behind; on a
    # large repo that is hundreds of megabytes of nothing.
    con.execute("VACUUM")

    stats = summarize(con)
    stats.update({
        "db": db_path,
        "took": round(time.time() - t0, 2),
        "parsed": parsed,
        "cached": skipped,
        "removed": len(removed),
        "edges": n_edges,
    })
    for k in ("files", "code_files", "total_loc", "functions", "classes",
              "nodes", "edges"):
        if k in stats:
            _meta_set(con, "stat_" + k, stats[k])
    con.commit()
    con.close()
    return stats


def summarize(con):
    def scalar(q, *a):
        row = con.execute(q, a).fetchone()
        return row[0] if row else 0

    kinds = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM nodes GROUP BY kind")}
    ekinds = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM edges GROUP BY kind")}
    langs = {r["lang"]: r["n"] for r in con.execute(
        "SELECT lang, COUNT(*) n FROM files WHERE kind='code' "
        "GROUP BY lang ORDER BY n DESC")}
    return {
        "name": _meta_get(con, "name", "repository"),
        "root": _meta_get(con, "root", ""),
        "files": scalar("SELECT COUNT(*) FROM files"),
        "code_files": scalar("SELECT COUNT(*) FROM files WHERE kind='code'"),
        "total_loc": scalar("SELECT COALESCE(SUM(loc),0) FROM files "
                            "WHERE kind='code'"),
        "functions": kinds.get("Function", 0),
        "classes": kinds.get("Class", 0),
        "libraries": kinds.get("Library", 0),
        "dirs": kinds.get("Dir", 0),
        "nodes": sum(kinds.values()),
        "edges": sum(ekinds.values()),
        "node_kinds": kinds,
        "edge_kinds": ekinds,
        "languages": langs,
        "ambiguous_calls": int(_meta_get(con, "ambiguous_calls", 0) or 0),
        "compiler_calls": int(_meta_get(con, "compiler_calls", 0) or 0),
        "bound_calls": int(_meta_get(con, "bound_calls", 0) or 0),
        "external_calls": int(_meta_get(con, "external_calls", 0) or 0),
        "unparsed": scalar("SELECT COALESCE(SUM(unparsed),0) FROM files"),
        "files_with_unparsed": scalar(
            "SELECT COUNT(*) FROM files WHERE unparsed > 0"),
        "frontends": [(r["lang"], r["frontend"], r["n"]) for r in con.execute(
            "SELECT lang, frontend, COUNT(*) n FROM files WHERE kind='code' "
            "GROUP BY lang, frontend ORDER BY lang, n DESC")],
    }
