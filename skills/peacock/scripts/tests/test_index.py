"""
Tests for the agent-facing index and query layer.

Stdlib unittest only — same zero-dependency rule as the tool itself.

    python3 -m unittest discover -s tests -v

The tests that matter most are the property ones: stable IDs, honest
truncation, and scope-correct import resolution. Those are the three
assumptions everything else rests on, and all three are silent when they break
— a wrong edge or a quietly-cut list looks exactly like a right one.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.fmt import Budget                                     # noqa: E402
from engine.index import (build_index, connect, sym_id,           # noqa: E402
                          _library_name, _resolve_import,
                          _build_path_lookup, Ignore)
from engine.query import QueryError, COMMANDS, run                # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class Fixture(unittest.TestCase):
    """A small polyglot repo with the collisions that break naive resolvers."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peacock-test-")
        write(self.root, "app/main.py", (
            "from .helpers import compute\n"
            "import os\n"
            "\n"
            "def run():\n"
            "    '''Entry point.'''\n"
            "    return compute(3)\n"
            "\n"
            "def unused():\n"
            "    return 1\n"
        ))
        write(self.root, "app/helpers.py", (
            "def compute(n):\n"
            "    return double(n)\n"
            "\n"
            "def double(n):\n"
            "    return n * 2\n"
        ))
        # Same basename as app/helpers.py, different language and tree. A
        # basename-first resolver links main.py to this by mistake.
        write(self.root, "vendorlib/helpers.rb", (
            "def compute(n)\n"
            "  n\n"
            "end\n"
        ))
        write(self.root, ".gitignore", "generated/\n*.tmp.py\n")
        write(self.root, "generated/build.py", "def compute(n):\n    return 0\n")
        write(self.root, "scratch.tmp.py", "def compute(n):\n    return 0\n")
        self.db = os.path.join(self.root, ".peacock", "index.db")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, **kw):
        return build_index(self.root, **kw)

    def con(self):
        return connect(self.db)


class TestIndexing(Fixture):

    def test_builds_and_finds_symbols(self):
        st = self.build()
        self.assertEqual(st["code_files"], 3)          # generated/ + .tmp excluded
        con = self.con()
        names = {r[0] for r in con.execute(
            "SELECT name FROM nodes WHERE kind='Function'")}
        self.assertLessEqual({"run", "compute", "double", "unused"}, names)
        con.close()

    def test_gitignore_excludes_generated_output(self):
        self.build()
        con = self.con()
        paths = {r[0] for r in con.execute("SELECT path FROM files")}
        self.assertNotIn("generated/build.py", paths)
        self.assertNotIn("scratch.tmp.py", paths)
        self.assertIn("app/main.py", paths)
        con.close()

    def test_no_layout_data_in_index(self):
        """Render coordinates are pure overhead in an agent's context."""
        self.build()
        con = self.con()
        cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
        self.assertFalse(cols & {"x", "y", "z", "ax", "ay", "size"})
        con.close()


class TestStableIdentity(Fixture):

    def test_id_survives_edits_above_it(self):
        """The whole point of keeping line numbers out of the id."""
        self.build()
        con = self.con()
        before = {r[0] for r in con.execute(
            "SELECT id FROM nodes WHERE path='app/helpers.py'")}
        line_before = con.execute(
            "SELECT line FROM nodes WHERE id=?",
            (sym_id("app/helpers.py", "double"),)).fetchone()[0]
        con.close()

        with open(os.path.join(self.root, "app/helpers.py")) as fh:
            body = fh.read()
        write(self.root, "app/helpers.py",
              "# a new comment line\n# and another\n" + body)

        self.build()
        con = self.con()
        after = {r[0] for r in con.execute(
            "SELECT id FROM nodes WHERE path='app/helpers.py'")}
        line_after = con.execute(
            "SELECT line FROM nodes WHERE id=?",
            (sym_id("app/helpers.py", "double"),)).fetchone()[0]
        con.close()

        self.assertEqual(before, after, "ids must not change when lines shift")
        self.assertEqual(line_after, line_before + 2,
                         "line must track the edit as a mutable attribute")

    def test_repeated_names_get_distinct_ids(self):
        write(self.root, "app/dup.py",
              "def handle():\n    pass\n\ndef handle():\n    pass\n")
        self.build()
        con = self.con()
        ids = {r[0] for r in con.execute(
            "SELECT id FROM nodes WHERE path='app/dup.py' AND name='handle'")}
        con.close()
        self.assertEqual(len(ids), 2)


class TestIncremental(Fixture):

    def test_unchanged_files_are_reused(self):
        self.build()
        st = self.build()
        self.assertEqual(st["parsed"], 0)
        self.assertEqual(st["cached"], 3)

    def test_changed_file_is_reparsed(self):
        self.build()
        write(self.root, "app/main.py",
              "def run():\n    return 42\n\ndef added():\n    return 1\n")
        st = self.build()
        self.assertEqual(st["parsed"], 1)
        con = self.con()
        names = {r[0] for r in con.execute(
            "SELECT name FROM nodes WHERE path='app/main.py'")}
        con.close()
        self.assertIn("added", names)
        self.assertNotIn("unused", names, "stale symbols must be dropped")

    def test_deleted_file_is_forgotten(self):
        self.build()
        os.remove(os.path.join(self.root, "app/helpers.py"))
        st = self.build()
        self.assertEqual(st["removed"], 1)
        con = self.con()
        left = con.execute(
            "SELECT COUNT(*) FROM nodes WHERE path='app/helpers.py'").fetchone()[0]
        dangling = con.execute(
            "SELECT COUNT(*) FROM edges WHERE dst LIKE 's:app/helpers.py%'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(left, 0)
        self.assertEqual(dangling, 0, "edges into a deleted file must go too")


class TestResolution(Fixture):

    def test_relative_import_beats_basename_collision(self):
        """`from .helpers import x` in app/ must not find vendorlib/helpers.rb."""
        self.build()
        con = self.con()
        row = con.execute(
            "SELECT resolved FROM imports WHERE path='app/main.py' "
            "AND raw='.helpers'").fetchone()
        con.close()
        self.assertEqual(row["resolved"], "app/helpers.py")

    def test_ambiguous_basename_is_rejected(self):
        lookup = _build_path_lookup(["a/thing.py", "b/thing.py", "c/other.py"])
        self.assertIsNone(_resolve_import("thing", "z/caller.py", lookup))
        self.assertEqual(_resolve_import("other", "z/caller.py", lookup),
                         "c/other.py")

    def test_library_names_must_look_like_identifiers(self):
        self.assertIsNone(_library_name("#66b8ff"))
        self.assertIsNone(_library_name("from"))
        self.assertIsNone(_library_name(""))
        self.assertEqual(_library_name("os.path"), "os")
        self.assertEqual(_library_name("@scope/pkg"), "@scope")

    def test_call_edges_carry_confidence(self):
        # The name-matching tiers; the regex parser is the path that uses them.
        with mock.patch.dict(os.environ, {"PEACOCK_FRONTENDS": "regex"}):
            self.build(force=True)
        con = self.con()
        rows = {(r["src"], r["dst"]): r["conf"] for r in con.execute(
            "SELECT src, dst, conf FROM edges WHERE kind='calls'")}
        con.close()
        same_file = rows.get((sym_id("app/helpers.py", "compute"),
                              sym_id("app/helpers.py", "double")))
        self.assertEqual(same_file, 0.95, "same-file calls are certain")
        via_import = rows.get((sym_id("app/main.py", "run"),
                               sym_id("app/helpers.py", "compute")))
        self.assertEqual(via_import, 0.80, "resolved through an import")


class TestIgnoreMatcher(unittest.TestCase):

    def _ig(self, patterns, at="."):
        ig = Ignore()
        ig.rules = {}
        import tempfile as t
        d = t.mkdtemp()
        with open(os.path.join(d, ".gitignore"), "w") as fh:
            fh.write("\n".join(patterns))
        ig.load_dir(at, d)
        shutil.rmtree(d, ignore_errors=True)
        return ig

    def test_directory_only_pattern(self):
        ig = self._ig(["build/"])
        self.assertTrue(ig.ignored("build", True))
        self.assertTrue(ig.ignored("build/x.py", False))

    def test_anchored_vs_floating(self):
        ig = self._ig(["/root.py", "any.py"])
        self.assertTrue(ig.ignored("root.py", False))
        self.assertFalse(ig.ignored("sub/root.py", False))
        self.assertTrue(ig.ignored("sub/deep/any.py", False))

    def test_negation_last_match_wins(self):
        ig = self._ig(["*.log", "!keep.log"])
        self.assertTrue(ig.ignored("a.log", False))
        self.assertFalse(ig.ignored("keep.log", False))

    def test_star_does_not_cross_separator(self):
        ig = self._ig(["src/*.py"])
        self.assertTrue(ig.ignored("src/a.py", False))
        self.assertFalse(ig.ignored("src/deep/a.py", False))


class TestFabricatedEdges(unittest.TestCase):
    """Regressions for edges the graph used to invent.

    Every case here previously produced a *confident* wrong answer — the worst
    class of bug for an agent, because nothing downstream can detect it.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peacock-edge-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def index(self):
        build_index(self.root)
        return connect(os.path.join(self.root, ".peacock", "index.db"))

    def callers_of(self, con, name):
        return [r[0] for r in con.execute(
            "SELECT src FROM edges e JOIN nodes n ON n.id=e.dst "
            "WHERE e.kind='calls' AND n.name=?", (name,))]

    def test_doc_comment_does_not_create_a_call_edge(self):
        write(self.root, "App.java", (
            "public class App {\n"
            "\tprotected void refresh(Context c) {\n"
            "\t\tc.start();\n"
            "\t}\n"
            "\t/**\n"
            "\t * @see ConfigurableApplicationContext#refresh()\n"
            "\t */\n"
            "\tprotected void other(Context c) {\n"
            "\t\tc.noop();\n"
            "\t}\n"
            "}\n"))
        con = self.index()
        try:
            self.assertEqual(self.callers_of(con, "refresh"), [],
                             "a Javadoc @see must not become a call edge")
        finally:
            con.close()

    def test_string_literal_does_not_create_a_call_edge(self):
        write(self.root, "s.py", (
            "def target():\n    return 1\n\n"
            "def caller():\n    return 'call target() here'\n"))
        con = self.index()
        try:
            self.assertEqual(self.callers_of(con, "target"), [])
        finally:
            con.close()

    def test_multiline_signature_is_parsed(self):
        write(self.root, "W.java", (
            "public class W {\n"
            "\tprivate Environment prepareEnvironment(\n"
            "\t\t\tListeners listeners,\n"
            "\t\t\tArguments args) {\n"
            "\t\treturn null;\n"
            "\t}\n"
            "}\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute("SELECT name FROM nodes")}
            self.assertIn("prepareEnvironment", names,
                          "a wrapped signature must still yield a symbol")
        finally:
            con.close()

    def test_import_does_not_cross_languages(self):
        write(self.root, "fmt.py", "def helper():\n    return 1\n")
        write(self.root, "main.go",
              'package main\nimport "fmt"\nfunc Run() { fmt.Println("x") }\n')
        con = self.index()
        try:
            row = con.execute(
                "SELECT resolved FROM imports WHERE path='main.go'").fetchone()
            self.assertIsNone(row["resolved"],
                              "Go cannot import a Python module")
        finally:
            con.close()

    def test_method_name_collision_yields_no_edge(self):
        write(self.root, "m.py", (
            "class Alpha:\n    def handle(self):\n        return 1\n\n"
            "class Beta:\n    def handle(self):\n        return 2\n\n"
            "def driver(b):\n    return b.handle()\n"))
        con = self.index()
        try:
            self.assertEqual(
                self.callers_of(con, "handle"), [],
                "b.handle() names two candidates and must resolve to neither")
        finally:
            con.close()

    def test_constructor_call_is_an_edge(self):
        write(self.root, "c.py",
              "class Widget:\n    pass\n\ndef build():\n    return Widget()\n")
        con = self.index()
        try:
            self.assertEqual(len(self.callers_of(con, "Widget")), 1,
                             "constructing a class is a call")
        finally:
            con.close()

    def test_trailing_module_code_is_not_the_last_function(self):
        write(self.root, "t.py", (
            "def helper():\n    return 1\n\n"
            "def only_fn():\n    return 0\n\n"
            "helper()\n"))
        con = self.index()
        try:
            # Module-level code belongs to the file, never to the last
            # function above it.
            callers = con.execute(
                "SELECT src FROM edges WHERE kind='calls' AND dst LIKE '%#helper'"
            ).fetchall()
            self.assertNotIn("s:t.py#only_fn", [r[0] for r in callers],
                             "module-level code is not the last function")
        finally:
            con.close()

    def test_degrees_exclude_containment(self):
        write(self.root, "d.py", "def lonely():\n    return 1\n")
        con = self.index()
        try:
            indeg = con.execute(
                "SELECT indeg FROM nodes WHERE name='lonely'").fetchone()[0]
            self.assertEqual(indeg, 0,
                             "an uncalled function must read <-0, not <-1")
        finally:
            con.close()

    def test_comment_marker_inside_a_string_does_not_eat_the_file(self):
        """A `/*` in a Java literal used to open a block comment.

        Everything after it was swallowed until the next `*/`, and the damage
        was worse than omission: with the same-file definition deleted, calls
        to it resolved to an imported symbol of the same name instead. One
        dropped `load()` produced 80 fabricated callers on another class.
        """
        write(self.root, "A.java", (
            "public class A {\n"
            '\tvoid alpha() { load("classpath*:org/springframework/*.class"); }\n'
            '\tvoid beta() { System.out.println("hi"); }\n'
            '\tprivate String gamma(int x) { return "g"; }\n'
            "}\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM nodes WHERE path='A.java'")}
            for expected in ("alpha", "beta", "gamma"):
                self.assertIn(expected, names)
        finally:
            con.close()

    def test_annotation_type_and_constructor_are_symbols(self):
        write(self.root, "Ann.java",
              "public @interface Marker {\n\tString value();\n}\n")
        write(self.root, "C.java",
              "public class C {\n\tpublic C(int y) { this.y = y; }\n}\n")
        con = self.index()
        try:
            names = {r[0] for r in con.execute("SELECT name FROM nodes")}
            self.assertIn("Marker", names, "@interface must be indexed")
            self.assertIn("C", names, "a constructor must be indexed")
        finally:
            con.close()

    def test_package_qualified_import_needs_the_package_to_match(self):
        """`org.apache.commons.logging.Log` must not bind any lone Log.java."""
        write(self.root, "cli/util/Log.java",
              "package cli.util;\npublic class Log { void w() {} }\n")
        write(self.root, "User.java",
              "import org.apache.commons.logging.Log;\n"
              "public class User { void u() {} }\n")
        con = self.index()
        try:
            row = con.execute(
                "SELECT resolved FROM imports WHERE path='User.java'").fetchone()
            self.assertIsNone(row["resolved"],
                              "a package path that does not match is external")
        finally:
            con.close()

    def test_package_named_env_is_not_mistaken_for_a_virtualenv(self):
        """`env`, `build`, `bin` and `target` are ordinary Java packages.

        Ignoring them by basename anywhere removed 245 .java files from
        spring-boot, including all of org.springframework.boot.env.
        """
        write(self.root, "org/boot/env/Factory.java",
              "public class Factory { void make() {} }\n")
        write(self.root, "org/boot/build/Helper.java",
              "public class Helper { void help() {} }\n")
        con = self.index()
        try:
            paths = {r[0] for r in con.execute("SELECT path FROM files")}
            self.assertIn("org/boot/env/Factory.java", paths)
            self.assertIn("org/boot/build/Helper.java", paths)
        finally:
            con.close()

    def test_build_output_beside_a_manifest_is_still_skipped(self):
        write(self.root, "pom.xml", "<project/>\n")
        write(self.root, "target/Generated.java", "public class Generated {}\n")
        write(self.root, "src/Real.java", "public class Real { void go() {} }\n")
        con = self.index()
        try:
            paths = {r[0] for r in con.execute("SELECT path FROM files")}
            self.assertNotIn("target/Generated.java", paths)
            self.assertIn("src/Real.java", paths)
        finally:
            con.close()

    def test_abstract_and_interface_methods_are_indexed(self):
        """They end in `;`, not `{`. 4,070 were invisible in spring-boot."""
        write(self.root, "Iface.java", (
            "public interface EnvironmentPostProcessor {\n"
            "\tvoid postProcessEnvironment(ConfigurableEnvironment e);\n"
            '\tdefault String describe() { return "x"; }\n'
            "}\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute("SELECT name FROM nodes")}
            self.assertIn("postProcessEnvironment", names)
            self.assertIn("describe", names)
        finally:
            con.close()

    def test_signature_with_wrapped_throws_is_indexed(self):
        write(self.root, "W.java", (
            "public class W {\n"
            "\tprotected void match(Reader reader, Factory factory)\n"
            "\t\t\tthrows IOException {\n"
            "\t\treader.read();\n"
            "\t}\n"
            "}\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute("SELECT name FROM nodes")}
            self.assertIn("match", names)
        finally:
            con.close()

    def test_statements_are_not_declarations(self):
        """Permitting a `;` terminator must not turn statements into symbols.

        Abstract methods end in `;`, so the pattern has to accept it — which
        also admits `return foo(x);`, `throw new Bar(y);` and `new Baz(z);`.
        These fabricated 17 symbols in SpringApplication.java alone.
        """
        write(self.root, "S.java", (
            "public class S {\n"
            "\tvoid go() {\n"
            "\t\treturn helper(1);\n"
            "\t}\n"
            "\tvoid stop() {\n"
            "\t\tthrow new IllegalStateException(\"x\");\n"
            "\t}\n"
            "\tvoid make() {\n"
            "\t\tnew StartupInfoLogger(this.klass).logStart();\n"
            "\t}\n"
            "}\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM nodes WHERE path='S.java'")}
            for fabricated in ("helper", "IllegalStateException",
                               "StartupInfoLogger"):
                self.assertNotIn(fabricated, names)
            self.assertLessEqual({"go", "stop", "make"}, names)
        finally:
            con.close()

    def test_indentation_is_not_a_return_type(self):
        """The worst fabrication bug this tool has had.

        The Java type/modifier character class contained `\\s`, so leading
        indentation alone satisfied "there is a return type here". Combined
        with the `;` terminator needed for abstract methods, every indented
        unqualified call statement became a function symbol — 25,002 of them,
        28.8% of the spring-boot Java index. A fabricated symbol counts as a
        parsed declaration, so parse-health reported the repo clean while
        `who-calls` lost 39% of its edges and `span` returned one line for 39%
        of Java methods.
        """
        write(self.root, "F.java", (
            "public class F {\n"
            "\tvoid real() {\n"
            "\t\tassertThat(x).isEqualTo(y);\n"
            "\t\tregister(bean);\n"
            "\t\tload(context);\n"
            "\t\tsynchronized (this.lock) {\n"
            "\t\t}\n"
            "\t}\n"
            "\tString alsoReal() { return null; }\n"
            "}\n"))
        con = self.index()
        try:
            names = sorted(r[0] for r in con.execute(
                "SELECT name FROM nodes WHERE path='F.java' "
                "AND kind IN ('Function','Class')"))
            self.assertEqual(names, ["F", "alsoReal", "real"],
                             f"statements became symbols: {names}")
        finally:
            con.close()

    def test_span_covers_a_body_containing_a_nested_definition(self):
        """`span` truncated at the next symbol, so any function holding an
        inner def or anonymous class returned only its signature — silently,
        at any budget, while AGENTS.md says to use span to read a symbol."""
        write(self.root, "n.py", (
            "def outer():\n"
            "    x = 1\n"
            "    def inner():\n"
            "        return 2\n"
            "    return inner() + x\n"
            "\n"
            "def after():\n"
            "    return 0\n"))
        con = self.index()
        try:
            length = con.execute(
                "SELECT length FROM nodes WHERE name='outer'").fetchone()[0]
            self.assertGreaterEqual(length, 5,
                                    "outer's body includes its nested def")
        finally:
            con.close()

    def test_rust_visibility_and_impl_blocks(self):
        write(self.root, "r.rs", (
            "pub const fn a() -> u8 { 0 }\n"
            "pub(crate) fn b() {}\n"
            'pub unsafe extern "C" fn c() {}\n'
            "pub struct Widget;\n"
            "impl Display for Widget {\n"
            "    fn fmt(&self) {}\n"
            "}\n"))
        con = self.index()
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM nodes WHERE path='r.rs'")]
            self.assertLessEqual({"a", "b", "c", "Widget", "fmt"}, set(names))
            self.assertEqual(names.count("Widget"), 1,
                             "an impl block must not duplicate the type name")
        finally:
            con.close()

    def test_java_text_block_does_not_fabricate_symbols(self):
        write(self.root, "T.java", (
            "public class T {\n"
            '\tprivate static final String TB = """\n'
            "\t\t\tpublic class NotReal {\n"
            "\t\t\t\tvoid alsoNotReal() {}\n"
            "\t\t\t}\n"
            '\t\t\t""";\n'
            "}\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute("SELECT name FROM nodes")}
            self.assertNotIn("NotReal", names)
            self.assertNotIn("alsoNotReal", names)
        finally:
            con.close()

    def test_kotlin_declarations(self):
        """Extension receiver, `enum class` and `internal class` all misparsed.

        The extension case was the worst: the receiver type landed in the name
        group, so one file produced 19 phantom functions named TestRestTemplate
        that collided with the real Java class of that name.
        """
        write(self.root, "E.kt", (
            "inline fun <reified T : Any> TestRestTemplate.getForObject("
            "url: String): T? {\n\treturn null\n}\n"
            "enum class Color { RED, GREEN }\n"
            "internal class Helper { fun go() {} }\n"))
        con = self.index()
        try:
            names = {r[0] for r in con.execute("SELECT name FROM nodes")}
            self.assertIn("getForObject", names)
            self.assertNotIn("TestRestTemplate", names)
            self.assertIn("Color", names)
            self.assertNotIn("class", names)
            self.assertIn("Helper", names)
        finally:
            con.close()

    def test_kotlin_can_import_java(self):
        write(self.root, "a/Thing.java", "package a;\npublic class Thing {}\n")
        write(self.root, "a/Use.kt", "package a\nimport a.Thing\nfun go() {}\n")
        con = self.index()
        try:
            row = con.execute(
                "SELECT resolved FROM imports WHERE path='a/Use.kt'").fetchone()
            self.assertEqual(row["resolved"], "a/Thing.java",
                             "Kotlin and Java share a compilation target")
        finally:
            con.close()

    def test_symlinked_file_is_not_indexed_twice(self):
        write(self.root, "real.py", "def only_once():\n    return 1\n")
        try:
            os.symlink(os.path.join(self.root, "real.py"),
                       os.path.join(self.root, "alias.py"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        con = self.index()
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM nodes WHERE name='only_once'").fetchone()[0]
            self.assertEqual(n, 1, "a duplicate loses the unique-name tier")
        finally:
            con.close()


    def test_id_never_silently_rebinds_to_other_source(self):
        """The critic's exact scenario: a duplicate name inserted above."""
        write(self.root, "t.py",
              "def alpha():\n    return 1\n\ndef process():\n    return alpha()\n")
        con = self.index()
        original = con.execute(
            "SELECT id FROM nodes WHERE name='process'").fetchone()[0]
        con.close()

        write(self.root, "t.py", "def process():\n    return 0\n\n"
                                 "def alpha():\n    return 1\n\n"
                                 "def process():\n    return alpha()\n")
        con = self.index()
        try:
            row = con.execute("SELECT line FROM nodes WHERE id=?",
                              (original,)).fetchone()
            if row is not None:
                self.assertNotEqual(
                    row["line"], 1,
                    "the old id must never resolve to the newly-inserted symbol")
        finally:
            con.close()


class TestParseHealth(unittest.TestCase):
    """The detector must not share the parser's blind spot.

    Its first version required a trailing `{` — exactly the condition whose
    absence caused the dominant miss — so it reported clean on 96.7% of files
    that provably had missing declarations, while firing on `else if`.
    """

    def check(self, source, language="java"):
        from engine.parser import (strip_noise, logical_lines, count_unparsed,
                                   parse_source)
        fi = parse_source("X." + {"java": "java", "python": "py"}[language],
                          source)
        clean = strip_noise(source, fi.language).splitlines()
        joined, consumed = logical_lines(clean)
        return count_unparsed(clean, {s.line for s in fi.symbols}, consumed)

    def test_control_flow_is_not_a_declaration(self):
        src = ("public class C {\n"
               "\tvoid go(int x) {\n"
               "\t\tif (x > 1) {\n"
               "\t\t} else if (x > 2) {\n"
               "\t\t}\n"
               "\t\treturn switch (x) {\n"
               "\t\t\tdefault -> 1;\n"
               "\t\t};\n"
               "\t}\n"
               "}\n")
        self.assertEqual(self.check(src), 0,
                         "`else if` and `return switch` are not declarations")

    def test_clean_file_reports_zero(self):
        src = ("public class C {\n"
               "\tvoid a() {}\n"
               "\tvoid b(int x) {}\n"
               "\tString c() { return null; }\n"
               "}\n")
        self.assertEqual(self.check(src), 0)

    def test_python_is_clean(self):
        src = ("def a():\n    return 1\n\n"
               "class B:\n    def c(self):\n        if True:\n"
               "            return 2\n")
        self.assertEqual(self.check(src, "python"), 0)


class TestQueryRobustness(Fixture):

    def setUp(self):
        super().setUp()
        self.build()
        self._con = self.con()

    def tearDown(self):
        self._con.close()
        super().tearDown()

    def test_exact_symbol_name_beats_a_path_suffix(self):
        """SQLite LIKE is case-insensitive, so a directory named
        `docs/features/springapplication` was shadowing the class
        `SpringApplication` — hiding 335 symbol names including `main`."""
        os.makedirs(os.path.join(self.root, "docs", "compute"), exist_ok=True)
        write(self.root, "docs/compute/notes.md", "# notes\n")
        self.build()
        con = self.con()
        try:
            data, _ = run(con, "find", target="compute")
            self.assertEqual(data["matches"][0]["kind"], "Function",
                             "the symbol must outrank the directory")
        finally:
            con.close()

    def test_like_wildcards_are_escaped(self):
        data, _ = run(self._con, "find", target="%")
        self.assertEqual(data["total"], 0,
                         "'%' must be a literal, not match everything")

    def test_bad_arguments_are_rejected(self):
        with self.assertRaises(QueryError):
            run(self._con, "impact", target="run", depth=-1)
        with self.assertRaises(QueryError):
            run(self._con, "overview", budget=0)

class TestTruncationHonesty(Fixture):
    """Every cap must announce itself, including caps below the budget layer."""

    def test_who_calls_reports_the_true_total(self):
        body = ["def target():\n    return 1\n"]
        for i in range(40):
            body.append(f"def caller{i}():\n    return target()\n")
        write(self.root, "app/many.py", "\n".join(body))
        self.build()
        con = self.con()
        try:
            data, _ = run(con, "who-calls", target="app/many.py:target")
            self.assertEqual(data["total"], 40)
            from engine.fmt import Budget
            from engine.query import COMMANDS
            b = Budget(120)
            COMMANDS["who-calls"][1](data, b)
            out = b.render()
            self.assertTrue("INCOMPLETE" in out or "budget reached" in out,
                            f"a cut list must say so, got:\n{out}")
        finally:
            con.close()

    def test_every_renderer_announces_truncation(self):
        """No command may drop content in silence — overview included."""
        from engine.fmt import Budget
        from engine.query import COMMANDS
        args = {
            "outline": {"target": "app/main.py"},
            "find": {"target": "compute"},
            "who-calls": {"target": "compute"},
            "calls": {"target": "run"},
            "neighbors": {"target": "run"},
            "deps": {"target": "app/main.py"},
            "dependents": {"target": "app/helpers.py"},
            "impact": {"target": "double"},
            "subgraph": {"seeds": ["run"]},
            "span": {"target": "run"},
        }
        self.build()
        con = self.con()
        try:
            for name, (qfn, rfn, _, _) in COMMANDS.items():
                data = qfn(con, **args.get(name, {}))
                b = Budget(70)
                rfn(data, b)
                if b.dropped:
                    with self.subTest(command=name):
                        self.assertIn("budget reached", b.render(),
                                      f"{name} truncated silently")
        finally:
            con.close()


class TestBudget(unittest.TestCase):

    def test_truncation_is_announced(self):
        b = Budget(120)
        for i in range(500):
            b.add(f"line {i} with a reasonable amount of padding text")
        b.note()
        out = b.render()
        self.assertIn("token budget reached", out,
                      "a silently truncated list is a correctness bug")
        self.assertGreater(b.dropped, 0)

    def test_stays_within_budget(self):
        for limit in (64, 200, 1000, 5000):
            b = Budget(limit)
            for i in range(5000):
                b.add(f"engine/query.py:{i} some_symbol_name c4 L20 <-2 ->3")
            b.note()
            from engine.fmt import est_tokens
            self.assertLessEqual(est_tokens(b.render()), limit * 1.1,
                                 f"overshot budget {limit}")

    def test_untruncated_output_has_no_notice(self):
        b = Budget(2000)
        b.add("one line")
        b.note()
        self.assertNotIn("budget reached", b.render())


class TestQueries(Fixture):

    def setUp(self):
        super().setUp()
        self.build()
        self._con = self.con()

    def tearDown(self):
        self._con.close()
        super().tearDown()

    def test_every_command_runs(self):
        args = {
            "outline": {"target": "app/main.py"},
            "find": {"target": "compute"},
            "who-calls": {"target": "compute"},
            "calls": {"target": "run"},
            "neighbors": {"target": "run"},
            "deps": {"target": "app/main.py"},
            "dependents": {"target": "app/helpers.py"},
            "impact": {"target": "double"},
            "subgraph": {"seeds": ["run"]},
            "span": {"target": "run"},
        }
        for name in COMMANDS:
            with self.subTest(command=name):
                data, text = run(self._con, name, budget=800,
                                 **args.get(name, {}))
                self.assertIsInstance(data, dict)
                self.assertIsInstance(text, str)

    def test_who_calls_finds_the_caller(self):
        _, text = run(self._con, "who-calls", target="app/helpers.py:compute")
        self.assertIn("app/main.py", text)
        self.assertIn("run", text)

    def test_impact_reaches_transitive_callers(self):
        """double <- compute <- run, across a file boundary."""
        data, _ = run(self._con, "impact", target="double", depth=3)
        reached = {s["name"] for items in data["by_file"].values()
                   for s in items}
        self.assertIn("compute", reached)
        self.assertIn("run", reached)

    def test_span_returns_only_that_symbol(self):
        data, text = run(self._con, "span", target="double")
        self.assertIn("def double", text)
        self.assertNotIn("def compute", text)

    def test_outline_lists_symbols_without_source(self):
        _, text = run(self._con, "outline", target="app/helpers.py")
        self.assertIn("compute", text)
        self.assertIn("double", text)
        self.assertNotIn("return n * 2", text, "outline must not dump source")

    def test_unknown_target_gives_actionable_error(self):
        with self.assertRaises(QueryError) as ctx:
            run(self._con, "who-calls", target="no_such_symbol_anywhere")
        self.assertIn("find", str(ctx.exception))

    def test_ambiguity_is_reported_not_hidden(self):
        write(self.root, "app/other.py", "def compute(n):\n    return 9\n")
        self.build()
        con = self.con()
        try:
            _, text = run(con, "who-calls", target="compute")
            self.assertIn("other symbols", text)
        finally:
            con.close()

    def test_overview_is_small(self):
        from engine.fmt import est_tokens
        _, text = run(self._con, "overview", budget=2000)
        self.assertLess(est_tokens(text), 600,
                        "overview must stay cheap enough to always run first")


if __name__ == "__main__":
    unittest.main()
