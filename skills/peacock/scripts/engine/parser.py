"""
Heuristic, dependency-free source parser.

Reads a single file and returns a FileInfo with the structural facts Peacock
needs: symbols (functions/classes), imports, and line-level metrics
(code / comment / blank lines, cyclomatic proxy, max nesting depth, docstring
and type-hint presence). Everything here is stdlib + regex only.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from .languages import (lang_for, LANGUAGES, GENERIC_BRANCH, DOC_EXTS,
                        CONFIG_EXTS)


def _unescape(pat):
    """Turn a simple regex literal like `/\\*` back into `/*`."""
    return re.sub(r"\\(.)", r"\1", pat)


# Keywords that begin a *statement*, never a declaration. See the guard in
# parse_source: without it, permitting a `;` terminator (needed so abstract and
# interface methods are visible) turns every `return foo(x);` into a symbol
# named foo.
STATEMENT_STARTERS = frozenset("""
return throw throws raise new delete yield await assert del print if else elif
for while do switch case break continue goto try catch except finally with
using lock defer go when guard match super this echo require include unset
""".split())

# Languages whose triple-quoted strings span lines. Treated as block delimiters
# so a docstring or text block never reaches the symbol or call-site scanners.
_TRIPLE = {
    "python": ('"""', "'''"),
    "java": ('"""',),      # text blocks (Java 15+)
    "kotlin": ('"""',),
    "scala": ('"""',),
    "groovy": ('"""', "'''"),
}


def strip_noise(text: str, language: str, strings: bool = True) -> str:
    """Blank out comments (and optionally string literals), preserving structure.

    Line count and column positions are kept intact so every line number
    computed against the result is still valid against the original.

    This exists because a scanner that cannot see comments will happily read
    code out of them. A Javadoc line like

        * @see ConfigurableApplicationContext#refresh()

    looks exactly like a call to `refresh()`, and produced a fabricated call
    edge attributed to whichever method the comment happened to precede — at
    the *highest* confidence tier, since the "definition" was in the same file.

    `strings=False` keeps literals, which import scanning needs: Go, JS, Ruby
    and friends name their targets *inside* a string, so blanking them removes
    the very thing being matched.
    """
    defn = LANGUAGES.get(language)
    if not defn:
        return text

    line_comments = list(defn.get("line_comment") or [])
    block = defn.get("block_comment")
    bopen, bclose = (_unescape(block[0]), _unescape(block[1])) if block else (None, None)
    triples = _TRIPLE.get(language, ())
    quotes = "\"'`"

    out = []
    state = None          # None | "block" | one of the triple markers
    for raw in text.splitlines():
        buf = []
        i, n = 0, len(raw)
        while i < n:
            if state == "block":
                idx = raw.find(bclose, i)
                if idx == -1:
                    buf.append(" " * (n - i))
                    i = n
                else:
                    buf.append(" " * (idx - i + len(bclose)))
                    i = idx + len(bclose)
                    state = None
                continue
            if state in triples:
                idx = raw.find(state, i)
                if idx == -1:
                    buf.append(" " * (n - i))
                    i = n
                else:
                    buf.append(" " * (idx - i + len(state)))
                    i = idx + len(state)
                    state = None
                continue

            ch = raw[i]
            tri = next((t for t in triples if raw.startswith(t, i)), None)
            if tri:
                end = raw.find(tri, i + len(tri))
                if end == -1:
                    buf.append(" " * (n - i))
                    i = n
                    state = tri
                else:
                    buf.append(" " * (end + len(tri) - i))
                    i = end + len(tri)
                continue
            if bopen and raw.startswith(bopen, i):
                end = raw.find(bclose, i + len(bopen))
                if end == -1:
                    buf.append(" " * (n - i))
                    i = n
                    state = "block"
                else:
                    buf.append(" " * (end + len(bclose) - i))
                    i = end + len(bclose)
                continue
            lc = next((c for c in line_comments if raw.startswith(c, i)), None)
            if lc:
                buf.append(" " * (n - i))
                i = n
                continue
            if ch in quotes and strings:
                j = i + 1
                while j < n:
                    if raw[j] == "\\":
                        j += 2
                        continue
                    if raw[j] == ch:
                        j += 1
                        break
                    j += 1
                buf.append(" " * (min(j, n) - i))
                i = min(j, n)
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return "\n".join(out)


def logical_lines(clean_lines, max_join=8):
    """Join declarations whose parameter list wraps onto later lines.

    Returns (joined, consumed). `joined[i]` is line i with its continuations
    appended; `consumed` holds the indices that were folded into an earlier
    line and must not be scanned for declarations themselves.

    Without this, every Java or Kotlin method whose signature does not fit on
    one line is invisible — and invisible in the worst way, because the file
    still reports a confident symbol count that simply omits them.
    """
    joined = list(clean_lines)
    consumed = set()
    n = len(clean_lines)
    for i in range(n):
        if i in consumed:
            continue
        line = clean_lines[i]
        if "(" not in line:
            continue

        parts, j, steps = [line], i + 1, 0
        depth = line.count("(") - line.count(")")
        while j < n and depth > 0 and steps < max_join:
            nxt = clean_lines[j]
            parts.append(nxt.strip())
            depth += nxt.count("(") - nxt.count(")")
            consumed.add(j)
            j += 1
            steps += 1
        if depth > 0:                       # never balanced — not a signature
            consumed.difference_update(range(i + 1, j))
            continue

        # A balanced parameter list is not always the end of the signature. A
        # `throws` clause routinely wraps to the next line, leaving the
        # declaration without the trailing `{` every pattern looks for; 221
        # spring-boot methods were invisible for exactly this reason. Only a
        # continuation that actually looks like one is absorbed, so ordinary
        # statements are left alone.
        while j < n and steps < max_join and not re.search(r"[{};]\s*$", parts[-1]):
            nxt = clean_lines[j].strip()
            if not nxt or not re.match(r"^(throws\b|implements\b|extends\b|\{)", nxt):
                break
            parts.append(nxt)
            consumed.add(j)
            j += 1
            steps += 1
        if len(parts) > 1:
            joined[i] = " ".join(parts)
    return joined, consumed


# Detecting what the parser missed, WITHOUT sharing the parser's blind spot.
#
# The first version of this check required a trailing `{` — the exact condition
# whose absence causes the dominant failure, since abstract and interface
# methods end in `;`. A detector written in the parser's own idiom can only
# find misses caused by something other than the parser's main weakness, so it
# reported clean on 96.7% of files that provably had missing declarations while
# firing on `else if (...) {` nine times out of ten. Silence from a check like
# that is worse than no check at all: it certifies a completeness it never
# verified, and the prompt tells agents to believe it.
#
# So this one accepts a declaration terminated by `{`, by `;`, or by nothing,
# and leans on an explicit control-flow exclusion rather than on punctuation.
_DECL_SHAPE = re.compile(
    r"^\s*(?:@\w+\s*(?:\([^)]*\))?\s*)*"          # leading annotations
    r"(?:[A-Za-z_$][\w<>\[\].,?$]*\s+)+"            # modifiers and return type
    r"([A-Za-z_$][\w$]*)\s*\([^;{]*\)\s*"          # name and parameter list
    r"(?:const\s*|noexcept\s*|throws[\w., ]*)?[{;]?\s*$")
_DECL_KEYWORD = re.compile(
    r"^\s*(?:(?:pub|public|private|protected|internal|static|final|abstract|"
    r"open|override|suspend|async|inline|export|default|sealed|data|value)\s+)*"
    r"(?:def|fun|func|fn|sub|class|struct|trait|impl|interface|enum|record|"
    r"object|module|protocol|extension)\s+[A-Za-z_$]")

# Keywords that take a parenthesised clause and are not declarations. Without
# these, `else if (x) {`, `return switch (x) {` and `catch (E e) {` all read as
# method declarations — 90% of the first version's output was exactly that.
_NOT_A_DECL = frozenset("""
if else elif for while switch case do try catch except finally return yield
throw throws raise synchronized assert with using lock when guard defer go
await new match del print exec eval super this self lambda not and or is in
""".split())


def count_unparsed(clean_lines, symbol_lines, consumed):
    """How many declaration-shaped lines produced no symbol.

    Deliberately rough, and biased toward over-reporting: a missed declaration
    does not merely shorten a symbol list, it manufactures a wrong edge
    elsewhere, because calls to the missing definition resolve to an imported
    symbol of the same name. A false alarm costs a grep; a false all-clear
    costs a wrong decision.
    """
    missed = 0
    for i, line in enumerate(clean_lines):
        if i in consumed or (i + 1) in symbol_lines:
            continue
        s = line.strip()
        if not s or s.startswith(("}", ")", "]", "*", "@", ".", "//")):
            continue
        head = s.split("(")[0].split()
        if head and head[0] in _NOT_A_DECL:
            continue
        m = _DECL_SHAPE.match(line)
        if m:
            if m.group(1) in _NOT_A_DECL:
                continue
            missed += 1
            continue
        if _DECL_KEYWORD.match(line):
            missed += 1
    return missed


@dataclass
class Symbol:
    name: str
    kind: str          # "function" | "class"
    line: int          # 1-based
    length: int = 1    # lines spanned (approx)
    complexity: int = 1
    documented: bool = False
    parent: str = None  # enclosing class, when a compiler front-end knows it


@dataclass
class FileInfo:
    path: str          # repo-relative, posix
    language: str
    loc: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    max_nesting: int = 0
    complexity: int = 0
    symbols: list = field(default_factory=list)
    imports: list = field(default_factory=list)
    has_type_hints: bool = False
    unparsed: int = 0    # declaration-shaped lines that produced no symbol
    kind: str = "code"   # "code" | "doc" | "config"
    error: str = ""
    frontend: str = "regex"   # which parser produced symbols: regex | ast | javac


def _strip_strings(line: str, defn) -> str:
    """Blank out string literals so their contents don't trip other regexes."""
    for pat in defn["string"]:
        line = re.sub(pat, '""', line)
    return line


def _indent_width(line: str) -> int:
    w = 0
    for ch in line:
        if ch == " ":
            w += 1
        elif ch == "\t":
            w += 4
        else:
            break
    return w


def parse_source(rel_path: str, text: str) -> FileInfo:
    name, defn = lang_for(rel_path)
    import os
    ext = os.path.splitext(rel_path)[1].lower()

    if defn is None:
        # Non-code files still contribute to project shape.
        if ext in DOC_EXTS:
            fi = FileInfo(path=rel_path, language="markdown", kind="doc")
        elif ext in CONFIG_EXTS:
            fi = FileInfo(path=rel_path, language="config", kind="config")
        else:
            return None
        lines = text.splitlines()
        fi.loc = len(lines)
        fi.blank_lines = sum(1 for l in lines if not l.strip())
        fi.code_lines = fi.loc - fi.blank_lines
        return fi

    fi = FileInfo(path=rel_path, language=name)
    lines = text.splitlines()
    fi.loc = len(lines)

    line_comments = defn["line_comment"]
    block = defn["block_comment"]
    block_open = re.compile(block[0]) if block else None
    block_close = re.compile(block[1]) if block else None
    in_block = False
    doc_marker = re.compile(defn["doc"]) if defn.get("doc") else None
    type_hint = re.compile(defn["type_hint"]) if defn.get("type_hint") else None

    func_res = [re.compile(p) for p in defn["func"]]
    cls_res = [re.compile(p) for p in defn["cls"]]
    imp_res = [re.compile(p) for p in defn["imp"]]

    symbols: list[Symbol] = []
    imports: list[str] = []
    seen_imports = set()

    total_complexity = 0
    max_nesting = 0

    # Declarations, imports and branch counting all run against source with
    # comments and string bodies blanked out, and with wrapped signatures
    # folded onto their first line. Comment/blank accounting below still uses
    # the raw text, since that is what it is counting.
    clean = strip_noise(text, name).splitlines()
    # Imports are matched against a comment-stripped but string-*preserving*
    # view, because in Go, JS, Ruby and others the module name lives inside a
    # string literal.
    no_comments = strip_noise(text, name, strings=False).splitlines()
    for buf in (clean, no_comments):
        while len(buf) < len(lines):
            buf.append("")
    joined, consumed = logical_lines(clean)

    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            fi.blank_lines += 1
            continue

        # Comment classification comes from the stripped view, which knows the
        # difference between a comment and a comment-shaped substring inside a
        # string. The previous per-line scanner did not: a Java literal such as
        #
        #     load("classpath*:org/springframework/*.class")
        #
        # opened a block comment that swallowed every declaration until the next
        # `*/`. 107 files in spring-boot contain such a literal, and the damage
        # was not just omission — with the same-file definition deleted, calls
        # to it resolved to an imported symbol of the same name instead, so one
        # dropped `load()` produced 80 fabricated callers on another class.
        code = clean[i - 1]
        if not code.strip():
            fi.comment_lines += 1
            continue
        fi.code_lines += 1

        # --- nesting via indentation (indent langs) or generic depth ---
        nd = _indent_width(raw) // 4
        if nd > max_nesting:
            max_nesting = nd

        # --- cyclomatic proxy ---
        branches = len(GENERIC_BRANCH.findall(code))
        total_complexity += branches

        # --- type hints ---
        if type_hint and not fi.has_type_hints and type_hint.search(code):
            fi.has_type_hints = True

        # --- imports ---
        for r in imp_res:
            m = r.search(no_comments[i - 1])
            if m:
                mod = m.group(1).strip()
                if mod and mod not in seen_imports:
                    seen_imports.add(mod)
                    imports.append(mod)

        if (i - 1) in consumed:
            continue
        decl = joined[i - 1]

        # Allowing a `;` terminator (so abstract and interface methods are
        # visible) also lets ordinary statements in: `return foo(x);`,
        # `throw new Bar(y);` and `new Baz(z);` all have the shape
        # "<tokens> name(...);". Rejecting lines that open with a statement
        # keyword is what keeps the `;` form from fabricating symbols — it was
        # inventing 17 of them in SpringApplication.java alone.
        lead = decl.strip().split("(")[0].split()
        if lead and lead[0] in STATEMENT_STARTERS:
            continue

        # --- classes ---
        matched = False
        for r in cls_res:
            m = r.match(decl)
            if m and m.group(1):
                symbols.append(Symbol(m.group(1), "class", i))
                matched = True
                break
        if matched:
            continue
        # --- functions ---
        for r in func_res:
            m = r.match(decl) or r.search(decl)
            if m and m.lastindex and m.group(1):
                fn = m.group(1)
                if fn in ("if", "for", "while", "switch", "catch", "return", "function"):
                    continue
                symbols.append(Symbol(fn, "function", i))
                break

    fi.symbols = symbols
    fi.unparsed = count_unparsed(clean, {sym.line for sym in symbols}, consumed)
    fi.imports = imports
    fi.complexity = total_complexity
    fi.max_nesting = max_nesting

    _annotate_symbol_spans(fi, lines, doc_marker, line_comments)
    return fi


def _annotate_symbol_spans(fi: FileInfo, lines, doc_marker, line_comments):
    """Approximate each symbol's length + local complexity from its span, and
    detect a leading docstring/doc-comment."""
    syms = fi.symbols
    n = len(lines)
    for idx, s in enumerate(syms):
        start = s.line
        end = syms[idx + 1].line - 1 if idx + 1 < len(syms) else n
        if end < start:
            end = start
        s.length = max(1, end - start + 1)
        body = "\n".join(lines[start - 1:end])
        s.complexity = 1 + len(GENERIC_BRANCH.findall(body))
        # docstring: next non-blank line after declaration starts a doc marker,
        # or the preceding line is a doc comment.
        if doc_marker:
            for j in range(start, min(start + 2, n)):
                if j < n and doc_marker.search(lines[j].strip()):
                    s.documented = True
                    break
            if not s.documented and start >= 2:
                prev = lines[start - 2].strip()
                if doc_marker.search(prev):
                    s.documented = True
        if not s.documented and line_comments and start >= 2:
            prev = lines[start - 2].strip()
            if any(prev.startswith(lc) for lc in line_comments):
                s.documented = True
