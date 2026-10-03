"""
The parser, run over 6.8 million lines of somebody else's code.

    PEACOCK_CORPUS=1 python3 -m unittest tests.test_corpus -v
    PEACOCK_CORPUS=1 PEACOCK_CORPUS_ONLY=go,rust python3 -m unittest tests.test_corpus
    PEACOCK_CORPUS=1 python3 tests/test_corpus.py --survey    # print the table

Skipped unless `PEACOCK_CORPUS=1`, because it downloads ~600 MB (once, cached in
`~/.cache/peacock-corpus`) and takes about ten minutes. `tests/corpus.py` holds
the manifest: nineteen repositories, one per supported language, each with more
than 2,000 GitHub stars and more than 20,000 lines of code, each pinned to a
commit SHA.

What this suite is for is the class of bug the unit tests structurally cannot
see. Every serious parser defect this project has shipped — a `/*` inside a
string literal swallowing 107 files, a directory-name rule deleting 245 more,
indentation acting as a return type and fabricating 25,002 symbols — was found
by running over a real repository and disbelieving the output, never by a
snippet. Snippets check that the regexes match what they were written for. This
checks what they do to a million lines they were not written for.

So the assertions here are deliberately of a different kind. Not "this file
contains this symbol" (that is `test_languages.py`) but:

  * the parser never raises, on any file, in any language;
  * a floor share of files in each language yields symbols at all;
  * no symbol is a statement keyword, a fragment, or on a line that does not
    contain it;
  * spot-checked declarations in real files are found, at their real line;
  * and the whole pipeline — index, then all thirteen query commands — runs on
    a third-party repository and stays inside its token budget.

The floors are measured, not aspirational: the table below records what the
parser actually does at these SHAs, and each threshold sits below it with
room. Several are low. `shell` is 49% because ohmyzsh writes `_omz::name()`
and the pattern cannot see a `::`; `r` is 3% on imports because R files rarely
call `library()`. A floor that flattered the parser would defeat the purpose of
having one — these numbers are here to be looked at, and the low ones are the
interesting ones.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.index import build_index, connect, discover, summarize   # noqa: E402
from engine.languages import lang_for                                # noqa: E402
from engine.parser import parse_source                               # noqa: E402
from engine.query import COMMANDS, run                               # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corpus                                                        # noqa: E402


# Measured 2026-08-10 at the pinned SHAs, over 6,840,898 lines. `observed` is
# what the parser did; the floors are what this suite will tolerate before it
# calls it a regression. Read the observed column as the honest coverage of a
# regex parser per language, not as a target.
#
#                      files with   files with   unparsed/     observed
#                      symbols ≥    imports ≥    symbols ≤     (sym / imp / unparsed)
FLOORS = {
    "python":     (60, 65,  5),    # 68.8 / 74.4 /  0.0   django
    "javascript": (65, 50, 10),    # 75.1 / 60.3 /  2.2   react
    "typescript": (70, 75, 10),    # 80.7 / 85.3 /  2.5   nest
    "java":       (80, 85,  5),    # 89.8 / 94.6 /  0.0   spring-boot
    "kotlin":     (82, 80,  5),    # 91.4 / 89.9 /  0.5   okhttp
    "go":         (88, 20,  8),    # 96.9 / 28.1 /  1.2   hugo
    "rust":       (85, 82, 20),    # 93.4 / 91.4 / 12.7   tokio
    "c":          (48, 75, 50),    # 57.3 / 85.8 / 41.2   redis
    "cpp":        (85, 90, 40),    # 94.3 /100.0 / 29.7   bitcoin
    "csharp":     (90, 78, 22),    # 99.2 / 87.0 / 15.0   jellyfin
    "ruby":       (88, 55,  8),    # 96.3 / 63.9 /  1.9   rails
    "php":        (85, 63,  5),    # 93.6 / 72.7 /  0.1   laravel
    "swift":      (86, 87, 10),    # 95.9 / 96.9 /  3.8   Alamofire
    "scala":      (90, 36,  8),    # 98.7 / 44.6 /  2.2   scala
    "dart":       (82, 83, 24),    # 91.2 / 92.4 / 16.7   flutter/packages
    "shell":      (40,  8, 12),    # 49.5 / 13.4 /  4.4   ohmyzsh
    "lua":        (45, 72, 12),    # 54.4 / 80.8 /  6.0   kong
    "r":          (50,  2,  5),    # 59.9 /  3.4 /  0.0   ggplot2
    "elixir":     (86, 64, 10),    # 95.2 / 73.0 /  3.8   elixir
}

# A symbol name is a node id in the index and a `find` target for an agent. It
# may be dotted (`Kong.init`, `Enum.EmptyError`), carry Ruby's `?`/`!`, or open
# with a dot — R's `.onLoad` and `.DollarNames.ggproto` are ordinary names —
# but a fragment or a bare number means a regex captured the wrong group.
IDENTIFIER = re.compile(r"^[.A-Za-z_$][\w$.:!?<>-]*$")

# One exception, understood and bounded. elixir-lang's own test suite contains
#
#     assert_raise ArgumentError, msg, fn ->
#       defmodule 1 + 2, do: :ok
#
# — a deliberately invalid module name, written to prove the compiler rejects
# it. `defmodule\s+([\w.]+)` captures the `1`. One symbol in 285,385 lines. It
# is listed rather than tolerated by a threshold so that a second one fails.
KNOWN_BAD_NAMES = {
    ("elixir", "lib/elixir/test/elixir/kernel_test.exs", "1"),
}

# Words that open a control-flow statement. Checked against the first token of
# a symbol's *declaration line*, never against the symbol's name.
#
# Checking names was the obvious design and it is wrong, which the corpus said
# immediately. Every keyword in one language is an ordinary identifier in the
# next: `except` is `Hash#except` in rails and a route helper in laravel, `try`
# is `Object#try` and a Kong PDK function, `finally` is a real Laravel batch
# method, `throw` and `continue` are functions in elixir's own standard
# library, `foreach` is one in the Lua C sources redis vendors, `break` and
# `foreach` are methods all over scala's test corpus, and django declares
# `def do`. 179 legitimate declarations across the corpus, all of which a
# name-based check would have called fabrications.
#
# The line's leading token has no such ambiguity. `def do(self):` starts with
# `def`; a fabricated symbol harvested from `if (ready) {` or
# `catch (Exception ex) {` starts with the keyword itself. That is exactly the
# failure this is looking for — the 25,002-symbol regression turned indented
# statements into declarations — and it holds in all nineteen languages.
CONTROL_FLOW = frozenset("""
if elif else for foreach while switch case do try catch except rescue finally
ensure return throw raise break continue goto with when unless until match
""".split())
LEADING_TOKEN = re.compile(r"[A-Za-z_]\w*")

# `go` and `c` deliberately sit at the bottom of the import and symbol tables
# respectively; see the module docstring and TestKnownLimitations in
# test_languages.py. The floors above encode that, rather than hiding it.


class Survey:
    """Everything one repository's parse produced, in aggregate."""

    def __init__(self, repo):
        self.repo = repo
        self.files = self.loc = self.symbols = 0
        self.functions = self.classes = self.imports = 0
        self.files_with_symbols = self.files_with_imports = 0
        self.unparsed = 0
        self.errors = []          # (path, exception) — must stay empty
        self.wrong_language = []  # (path, reported language)
        self.bad_names = []       # (path, name) — not identifier-shaped
        self.on_control_flow = []  # (path, name, line, text) — see below
        self.out_of_range = []    # (path, name, line) — line outside the file
        self.name_not_on_line = []
        self.anchors = {}         # (path, name) -> Symbol or None
        self.seconds = 0.0

    def pct(self, n):
        return 100.0 * n / self.files if self.files else 0.0

    @property
    def symbol_files_pct(self):
        return self.pct(self.files_with_symbols)

    @property
    def import_files_pct(self):
        return self.pct(self.files_with_imports)

    @property
    def unparsed_pct(self):
        return 100.0 * self.unparsed / self.symbols if self.symbols else 0.0

    def line(self):
        return (f"{self.repo.language:11} {self.repo.slug:28} "
                f"files={self.files:6} loc={self.loc:9} sym={self.symbols:7} "
                f"symfiles={self.symbol_files_pct:5.1f}% "
                f"impfiles={self.import_files_pct:5.1f}% "
                f"unparsed={self.unparsed_pct:5.1f}% {self.seconds:5.1f}s")


_CACHE = {}


def survey(repo):
    """Parse every file of `repo`'s language, once per process."""
    if repo.language in _CACHE:
        return _CACHE[repo.language]

    root = corpus.ensure_repo(repo)
    s = Survey(repo)
    wanted = {e.lower() for e in repo.exts}
    anchors = {(p, n): (k, None) for p, n, k in repo.anchors}
    t0 = time.time()

    for rel, ap in discover(root):
        ext = os.path.splitext(rel)[1].lower()
        if ext not in wanted:
            continue
        language, _ = lang_for(rel)
        if language is None:
            # Minified and generated files are skipped by extension rule, and
            # that rule is doing real work here: react ships 10 .min.js files,
            # each one line long and several hundred kilobytes wide.
            continue
        try:
            with open(ap, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            s.errors.append((rel, repr(exc)))
            continue

        try:
            fi = parse_source(rel, text)
        except Exception as exc:                       # noqa: BLE001
            s.errors.append((rel, repr(exc)))
            continue
        if fi is None:
            s.errors.append((rel, "parse_source returned None"))
            continue
        if fi.language != repo.language:
            s.wrong_language.append((rel, fi.language))

        lines = text.splitlines()
        s.files += 1
        s.loc += fi.loc
        s.symbols += len(fi.symbols)
        s.functions += sum(1 for x in fi.symbols if x.kind == "function")
        s.classes += sum(1 for x in fi.symbols if x.kind == "class")
        s.imports += len(fi.imports)
        s.unparsed += fi.unparsed
        s.files_with_symbols += 1 if fi.symbols else 0
        s.files_with_imports += 1 if fi.imports else 0

        for sym in fi.symbols:
            if (not IDENTIFIER.match(sym.name)
                    and (repo.language, rel, sym.name) not in KNOWN_BAD_NAMES):
                s.bad_names.append((rel, sym.name))
            if not 1 <= sym.line <= max(1, len(lines)):
                s.out_of_range.append((rel, sym.name, sym.line))
                continue
            text_line = lines[sym.line - 1].strip()
            head = LEADING_TOKEN.match(text_line)
            if head and head.group(0) in CONTROL_FLOW:
                s.on_control_flow.append(
                    (rel, sym.name, sym.line, text_line[:70]))
            leaf = sym.name.split(".")[-1].split("::")[-1]
            if leaf not in lines[sym.line - 1]:
                s.name_not_on_line.append((rel, sym.name, sym.line))
            key = (rel, sym.name)
            if key in anchors and anchors[key][1] is None:
                anchors[key] = (anchors[key][0], sym)

    s.anchors = anchors
    s.seconds = time.time() - t0
    _CACHE[repo.language] = s
    return s


def sample(rows, n=5):
    return ", ".join(str(r) for r in rows[:n])


@unittest.skipUnless(corpus.ENABLED,
                     "set PEACOCK_CORPUS=1 to run against real repositories")
class TestCorpus(unittest.TestCase):
    """One subTest per language, over the whole of each repository."""

    def repos(self):
        return corpus.selected()

    def test_repositories_are_as_large_as_claimed(self):
        """The selection rule is asserted from the checkout, not trusted.

        "More than 20,000 lines" is the constraint this corpus was picked
        under. Counting it here means the claim cannot quietly stop being true
        when a pin moves.
        """
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                self.assertGreater(s.loc, 20_000,
                                   f"{repo.slug} is smaller than the corpus rule")
                self.assertGreaterEqual(s.loc, repo.min_loc)
                self.assertGreater(s.files, 20)

    def test_the_parser_never_raises(self):
        """Every file in every language, parsed without an exception.

        The parser has no try/except around it in `index.py` — `_parse_one`
        catches OSError and decoding, not logic errors. One IndexError in a
        regex path takes down an index build over a repo the user cannot
        change.
        """
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                self.assertEqual(s.errors, [], f"{sample(s.errors)}")

    def test_language_detection_is_consistent(self):
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                self.assertEqual(s.wrong_language, [],
                                 f"misdetected: {sample(s.wrong_language)}")

    def test_files_yield_symbols(self):
        """A floor on how much of each repository the parser can see."""
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                floor = FLOORS[repo.language][0]
                self.assertGreaterEqual(
                    s.symbol_files_pct, floor,
                    f"{repo.slug}: only {s.symbol_files_pct:.1f}% of "
                    f"{s.files} files produced a symbol (floor {floor}%)")

    def test_files_yield_imports(self):
        """A floor on dependency edges — the half of the graph symbols miss."""
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                floor = FLOORS[repo.language][1]
                self.assertGreaterEqual(
                    s.import_files_pct, floor,
                    f"{repo.slug}: only {s.import_files_pct:.1f}% of "
                    f"{s.files} files produced an import (floor {floor}%)")

    def test_no_symbol_comes_from_a_control_flow_line(self):
        """A declaration never starts with `if`, `catch`, `for` or `return`.

        This is the shape of the worst regression this parser has had: a `;`
        terminator plus a permissive type character class turned every indented
        statement into a function and fabricated 25,002 symbols across
        spring-boot, 28.8% of the Java index. It was invisible in aggregate — a
        fabricated symbol counts as a *parsed* declaration, so parse-health
        scored the repo clean and the caller counts built on it were precisely
        wrong. Reading the line each symbol claims to be on is the cheapest
        detector for the whole family, and it needs no per-language grammar.
        """
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                self.assertEqual(
                    s.on_control_flow, [],
                    f"{len(s.on_control_flow)} symbols harvested from "
                    f"control flow: {sample(s.on_control_flow)}")

    def test_symbol_names_are_identifier_shaped(self):
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                self.assertEqual(s.bad_names, [],
                                 f"{len(s.bad_names)} malformed names: "
                                 f"{sample(s.bad_names)}")

    def test_symbol_lines_are_inside_the_file(self):
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                self.assertEqual(s.out_of_range, [],
                                 f"lines outside the file: "
                                 f"{sample(s.out_of_range)}")

    def test_anchor_declarations_are_found(self):
        """Named declarations in real files, at their real lines.

        Aggregates can stay green while a language quietly degrades — "90% of
        files yielded a symbol" says nothing about *which*. Each anchor names a
        specific declaration in a specific shape.
        """
        for repo in self.repos():
            s = survey(repo)
            for (path, name), (kind, sym) in sorted(s.anchors.items()):
                with self.subTest(language=repo.language, path=path, symbol=name):
                    self.assertIsNotNone(
                        sym, f"{repo.slug}: {name} not found in {path}")
                    self.assertEqual(sym.kind, kind)
                    self.assertGreater(sym.line, 0)

    def test_parse_health_stays_proportionate(self):
        """`unparsed` over-reports on purpose, but must stay readable.

        The check is biased toward over-reporting — a false alarm costs a grep,
        a false all-clear costs a wrong decision. The ceilings here are where
        that bias stops being useful: C is at 41% because a header full of
        prototypes is declaration-shaped and never a definition, and that is
        the honest number to know.
        """
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                ceiling = FLOORS[repo.language][2]
                self.assertLessEqual(
                    s.unparsed_pct, ceiling,
                    f"{repo.slug}: {s.unparsed_pct:.1f}% unparsed-to-symbol "
                    f"ratio (ceiling {ceiling}%)")

    def test_declaration_lines_carry_the_symbol_name(self):
        """A symbol's recorded line should be the line its name is on.

        Not zero tolerance: a signature that breaks between the return type and
        the name genuinely puts the name on line two, and the folded line still
        starts at line one. The bound is on how common that is, because line
        numbers are what `span`, `outline` and the editor jump all trust.
        """
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                s = survey(repo)
                rate = 100.0 * len(s.name_not_on_line) / max(1, s.symbols)
                self.assertLessEqual(
                    rate, 2.0,
                    f"{repo.slug}: {rate:.2f}% of symbols sit on a line that "
                    f"does not contain their name: {sample(s.name_not_on_line)}")

    def test_minified_and_generated_files_are_excluded(self):
        """`.min.js`, `.d.ts` and lockfiles are skipped by rule, on real data."""
        for repo in self.repos():
            with self.subTest(language=repo.language, repo=repo.slug):
                root = corpus.ensure_repo(repo)
                for rel, _ in discover(root):
                    low = rel.lower()
                    if low.endswith((".min.js", ".d.ts", "-lock.json")):
                        self.assertEqual(lang_for(rel), (None, None),
                                         f"{rel} should not parse as code")


@unittest.skipUnless(corpus.ENABLED,
                     "set PEACOCK_CORPUS=1 to run against real repositories")
class TestEndToEndOnRealRepository(unittest.TestCase):
    """Index a third-party repository and run every query against it.

    The parser tests above stop at `FileInfo`. This one goes through
    `build_index` — discovery, the SQLite store, import resolution, call
    resolution, degrees — and then runs all thirteen commands, because an agent
    never touches the parser. Alamofire is the smallest repo in the corpus
    (98 files, 43k lines), which keeps this to a few seconds.
    """

    @classmethod
    def setUpClass(cls):
        cls.repo = corpus.BY_LANGUAGE["swift"]
        if corpus.ONLY and cls.repo.language not in corpus.ONLY:
            raise unittest.SkipTest("swift not selected")
        cls.root = corpus.ensure_repo(cls.repo)
        cls.tmp = tempfile.mkdtemp(prefix="peacock-e2e-")
        cls.db = os.path.join(cls.tmp, "index.db")
        cls.summary = build_index(cls.root, out=cls.db)
        cls.con = connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.con.close()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_index_has_the_shape_of_a_swift_project(self):
        info = summarize(self.con)
        self.assertGreater(info["code_files"], 50)
        self.assertGreater(info["total_loc"], 20_000)
        self.assertIn("swift", info["languages"])

    def test_every_command_runs_on_foreign_code(self):
        """Thirteen commands, real repository, no crashes and no empty output."""
        target = "Session"
        for name, (_, _, args, _) in sorted(COMMANDS.items()):
            with self.subTest(command=name):
                kwargs = {}
                if "target" in args:
                    kwargs["target"] = target
                if "seeds" in args:
                    kwargs["seeds"] = [target]
                _, text = run(self.con, name, **kwargs)
                self.assertTrue(text.strip(), f"{name} produced nothing")

    def test_a_known_symbol_is_findable_and_spannable(self):
        _, found = run(self.con, "find", target="URLEncodedFormEncoder")
        self.assertIn("URLEncodedFormEncoder", found)
        _, span = run(self.con, "span", target="URLEncodedFormEncoder")
        self.assertIn("URLEncodedFormEncoder", span)

    def test_overview_stays_inside_its_budget(self):
        """The first command an agent runs, on a repo it has never seen."""
        _, text = run(self.con, "overview", budget=2000)
        self.assertLessEqual(len(text) // 4, 2000)
        self.assertIn("swift", text.lower())

    def test_truncation_announces_itself_on_a_real_repo(self):
        """A cut list must say it was cut — on foreign code as much as fixtures."""
        _, text = run(self.con, "find", target="a", budget=60)
        self.assertRegex(text, r"budget reached|INCOMPLETE|cut")

    def test_reindex_is_incremental(self):
        """Re-indexing an untouched repo re-parses nothing.

        The reader is closed first because `build_index` ends with a VACUUM,
        which cannot run while another connection holds the database.
        """
        self.con.close()
        try:
            again = build_index(self.root, out=self.db)
            self.assertEqual(again["parsed"], 0)
            self.assertGreater(again["cached"], 0,
                               "an unchanged repository should reuse its cache")
        finally:
            type(self).con = connect(self.db)


def print_survey():
    total_loc = total_sym = 0
    for repo in corpus.selected():
        s = survey(repo)
        print(s.line(), flush=True)
        total_loc += s.loc
        total_sym += s.symbols
    print(f"{'TOTAL':11} {'':28} {'':6}  loc={total_loc:9} sym={total_sym:7}")


if __name__ == "__main__":
    if "--survey" in sys.argv:
        if not corpus.ENABLED:
            sys.exit("set PEACOCK_CORPUS=1 first")
        print_survey()
    else:
        unittest.main(verbosity=2)
