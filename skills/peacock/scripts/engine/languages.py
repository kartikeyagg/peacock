"""
Language definitions for Peacock's heuristic, dependency-free parser.

Each language entry is a small bundle of regexes and token sets. The goal is
NOT a perfect parser (that would need a real grammar per language); it is a
robust, fast, good-enough extractor of the structures that matter for
understanding a codebase: functions, classes/types, imports and comments.

The parser degrades gracefully: an unknown extension still gets generic
line/branch/nesting metrics via the DEFAULT profile, so the tool is genuinely
language-free.
"""
from __future__ import annotations
import re

# Directories that are never source, wherever they appear. These names do not
# occur as package names in practice, so matching them by basename anywhere in
# the tree is safe.
IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", ".idea", ".vscode", ".next", ".nuxt", ".cache",
    "site-packages", ".gradle", ".dart_tool", "Pods", ".terraform",
    "bower_components", "jspm_packages", ".serverless", ".parcel-cache",
}

# Build-output names that are ALSO ordinary package names. `build`, `out`,
# `bin`, `target` and `env` are all real Java packages — ignoring them by
# basename anywhere removed 245 .java files from spring-boot, including the
# whole of org.springframework.boot.env, and `outline` on those returned "no
# match", which is indistinguishable from a typo.
#
# So these are ignored only where they are plausibly build output: directly
# beside a build manifest (a `target/` next to a pom.xml), or, for virtualenvs,
# only when the directory proves itself by containing pyvenv.cfg.
CONDITIONAL_IGNORE_DIRS = {
    "build", "out", "bin", "obj", "target", "dist", "coverage", "vendor",
}
BUILD_MANIFESTS = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "Makefile", "makefile", "package.json",
    "Cargo.toml", "go.mod", "build.sbt", "CMakeLists.txt", "meson.build",
}
VENV_DIRS = {"env", "venv", ".venv"}
VENV_MARKERS = {"pyvenv.cfg"}

# Files we skip even when the extension looks like code.
IGNORE_FILE_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock", ".bundle.js", "-lock.json",
    ".d.ts",
)

# Branch-introducing tokens used for a cheap cyclomatic-complexity proxy.
GENERIC_BRANCH = re.compile(
    r"\b(if|else\s+if|elif|for|foreach|while|case|when|catch|except|"
    r"rescue|and|or)\b|&&|\|\||\?\?|(?<![=!<>])\?(?!\?)"
)


def _langdef(**kw):
    base = {
        "func": [], "cls": [], "imp": [], "line_comment": [], "block_comment": None,
        "string": [r'"(?:\\.|[^"\\])*"', r"'(?:\\.|[^'\\])*'"],
        "doc": None, "type_hint": None,
    }
    base.update(kw)
    return base


# --- Per-language definitions -------------------------------------------------
LANGUAGES = {
    "python": _langdef(
        exts=[".py", ".pyw"],
        func=[r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("],
        cls=[r"^\s*class\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*import\s+([\w.]+)", r"^\s*from\s+([\w.]+)\s+import"],
        line_comment=["#"],
        doc=r'"""|\'\'\'',
        type_hint=r"->\s*[\w\[\], .]+:|:\s*[A-Z]\w+",
    ),
    "javascript": _langdef(
        exts=[".js", ".jsx", ".mjs", ".cjs"],
        func=[
            r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)",
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
            r"^\s*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
        ],
        cls=[r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"],
        imp=[r"import[^'\"]*['\"]([^'\"]+)['\"]", r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"/\*\*",
    ),
    "typescript": _langdef(
        exts=[".ts", ".tsx"],
        func=[
            r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)",
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=][^=]*=>",
            r"^\s*(?:public|private|protected|static|async|\s)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*[:{]",
        ],
        cls=[r"^\s*(?:export\s+)?(?:default\s+|abstract\s+)?(?:class|interface|enum|type)\s+([A-Za-z_$][\w$]*)"],
        imp=[r"import[^'\"]*['\"]([^'\"]+)['\"]", r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"/\*\*",
        type_hint=r":\s*[A-Za-z_$][\w$<>\[\], |]*",
    ),
    "java": _langdef(
        exts=[".java"],
        func=[
            # concrete methods; `;` covers abstract and interface declarations,
            # which have no body and were entirely invisible before
            r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|default|\s)*"
            r"[\w<>\[\].,?&\s]*[\w>\]?]\s+([A-Za-z_]\w*)\s*\([^;{]*\)\s*"
            r"(?:throws[\w., ]+)?(?:default\s[^;{]*)?[{;]",
            # constructors have no return type, so the rule above misses them
            r"^\s*(?:public|private|protected)?\s*([A-Z]\w*)\s*\([^;{]*\)\s*(?:throws[\w., ]+)?\{"],
        cls=[r"^\s*(?:public|private|protected|static|final|abstract|sealed|non-sealed|\s)*(?:@interface|class|interface|enum|record)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*import\s+(?:static\s+)?([\w.]+)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"/\*\*",
        type_hint=r"\b(?:int|long|double|float|boolean|String|char|byte|short|void|[A-Z]\w+)\b",
    ),
    "kotlin": _langdef(
        exts=[".kt", ".kts"],
        func=[r"^\s*(?:public|private|protected|internal|open|override|suspend|inline|operator|infix|tailrec|external|\s)*"
              r"fun\s+(?:<[^>]*>\s*)?"
              r"(?:[A-Za-z_]\w*(?:<[^>]*>)?\.)?"      # receiver type of an extension fn
              r"([A-Za-z_]\w*)"],
        cls=[r"^\s*(?:public|private|protected|internal|abstract|open|sealed|data|value|annotation|inner|enum|companion|\s)*(?:class|interface|object)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*import\s+([\w.]+)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"/\*\*",
    ),
    "go": _langdef(
        exts=[".go"],
        func=[r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"],
        cls=[r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)"],
        imp=[r'^\s*import\s+"([^"]+)"', r'^\s+_?\s*"([^"]+)"'],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"),
    ),
    "rust": _langdef(
        exts=[".rs"],
        func=[r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?"
              r"(?:async\s+)?(?:unsafe\s+)?(?:extern\s+(?:\"[^\"]*\"\s+)?)?"
              r"fn\s+([A-Za-z_]\w*)"],
        # `impl` blocks are deliberately NOT indexed as types. The struct or
        # trait declaration is the type; indexing the impl too created a second
        # node with the same name, which cost that name its unique-name tier in
        # call resolution.
        cls=[r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*use\s+([\w:]+)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"///|/\*\*",
        type_hint=r"->\s*[\w:<>&\[\], ]+|:\s*[A-Z]\w+",
    ),
    "c": _langdef(
        exts=[".c", ".h"],
        func=[r"^\s*(?:static\s+|inline\s+|extern\s+)*[\w*]+\s+\**([A-Za-z_]\w*)\s*\([^;]*\)\s*\{"],
        cls=[r"^\s*(?:typedef\s+)?(?:struct|union|enum)\s+([A-Za-z_]\w*)"],
        imp=[r'^\s*#\s*include\s+[<"]([^>"]+)[>"]'],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"),
    ),
    "cpp": _langdef(
        exts=[".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".c++"],
        func=[r"^\s*(?:[\w:<>*&,~\s]*[\w:>*&~]\s+)?([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const)?\s*(?:noexcept)?\s*\{"],
        cls=[r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+([A-Za-z_]\w*)"],
        imp=[r'^\s*#\s*include\s+[<"]([^>"]+)[>"]'],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"),
    ),
    "csharp": _langdef(
        exts=[".cs"],
        func=[r"^\s*(?:public|private|protected|internal|static|virtual|override|async|sealed|\s)*[\w<>\[\].,?]+\s+([A-Za-z_]\w*)\s*\([^;{]*\)\s*\{"],
        cls=[r"^\s*(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*(?:class|interface|struct|enum|record)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*using\s+(?:static\s+)?([\w.]+)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"///",
    ),
    "ruby": _langdef(
        exts=[".rb"],
        func=[r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?]?)"],
        cls=[r"^\s*(?:class|module)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]"],
        line_comment=["#"],
    ),
    "php": _langdef(
        exts=[".php"],
        func=[r"^\s*(?:public|private|protected|static|final|abstract|\s)*function\s+([A-Za-z_]\w*)"],
        cls=[r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*use\s+([\w\\]+)", r"(?:require|include)(?:_once)?\s*\(?\s*['\"]([^'\"]+)['\"]"],
        line_comment=["//", "#"], block_comment=(r"/\*", r"\*/"),
    ),
    "swift": _langdef(
        exts=[".swift"],
        func=[r"^\s*(?:public|private|internal|fileprivate|open|static|final|override|\s)*func\s+([A-Za-z_]\w*)"],
        cls=[r"^\s*(?:public|final|open|\s)*(?:class|struct|enum|protocol|extension)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*import\s+([\w.]+)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"),
    ),
    "scala": _langdef(
        exts=[".scala", ".sc"],
        func=[r"^\s*(?:private|protected|override|final|\s)*def\s+([A-Za-z_]\w*)"],
        cls=[r"^\s*(?:sealed\s+|abstract\s+|case\s+|final\s+)*(?:class|object|trait)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*import\s+([\w.]+)"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"),
    ),
    "dart": _langdef(
        exts=[".dart"],
        func=[r"^\s*(?:static\s+|final\s+)?[\w<>\[\], ?]*[\w>\]?]\s+([A-Za-z_]\w*)\s*\([^;{]*\)\s*(?:async\s*)?\{"],
        cls=[r"^\s*(?:abstract\s+)?(?:class|mixin|enum)\s+([A-Za-z_]\w*)"],
        imp=[r"^\s*import\s+['\"]([^'\"]+)['\"]"],
        line_comment=["//"], block_comment=(r"/\*", r"\*/"), doc=r"///",
    ),
    "shell": _langdef(
        exts=[".sh", ".bash", ".zsh"],
        func=[r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)\s*\{"],
        cls=[], imp=[r"^\s*(?:source|\.)\s+([^\s]+)"],
        line_comment=["#"],
    ),
    "lua": _langdef(
        exts=[".lua"],
        func=[r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)"],
        cls=[], imp=[r"require\s*\(?\s*['\"]([^'\"]+)['\"]"],
        line_comment=["--"],
    ),
    "r": _langdef(
        exts=[".r", ".R"],
        func=[r"^\s*([A-Za-z_.][\w.]*)\s*<-\s*function"],
        cls=[], imp=[r"(?:library|require)\s*\(\s*['\"]?([\w.]+)"],
        line_comment=["#"],
    ),
    "elixir": _langdef(
        exts=[".ex", ".exs"],
        func=[r"^\s*def(?:p)?\s+([A-Za-z_]\w*[!?]?)"],
        cls=[r"^\s*defmodule\s+([\w.]+)"],
        imp=[r"^\s*(?:import|alias|use|require)\s+([\w.]+)"],
        line_comment=["#"],
    ),
}

# Non-code files that still count toward "self-description" / project shape.
DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".gradle"}

DEFAULT = _langdef(exts=[], func=[], cls=[], imp=[], line_comment=["#", "//"])

# Build a fast extension -> (language_name, def) lookup.
EXT_MAP = {}
for _name, _d in LANGUAGES.items():
    for _e in _d["exts"]:
        EXT_MAP[_e] = (_name, _d)


def lang_for(path: str):
    """Return (language_name, definition) for a file path, or (None, None)."""
    lower = path.lower()
    for suf in IGNORE_FILE_SUFFIXES:
        if lower.endswith(suf):
            return (None, None)
    import os
    ext = os.path.splitext(path)[1].lower()
    # Preserve case only for the odd .R vs .r
    if ext in EXT_MAP:
        return EXT_MAP[ext]
    orig_ext = os.path.splitext(path)[1]
    if orig_ext in EXT_MAP:
        return EXT_MAP[orig_ext]
    return (None, None)
