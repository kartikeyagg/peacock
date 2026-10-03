"""
Tests for the compiler front-ends (engine/frontends.py).

    python3 -m unittest tests.test_frontends -v

Python's `ast` is always available. The javac tests skip when no JDK is on
PATH, which is also the configuration that must keep working via the regex
fallback.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import frontends                                      # noqa: E402
from engine.index import build_index, connect                     # noqa: E402
from engine.query import run                                      # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Repo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peacock-fe-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def index(self):
        build_index(self.root)
        return connect(os.path.join(self.root, ".peacock", "index.db"))

    def frontend_of(self, con, path):
        return con.execute("SELECT frontend FROM files WHERE path=?",
                           (path,)).fetchone()[0]

    def symbols(self, con, path):
        return [(r["name"], r["line"], r["length"]) for r in con.execute(
            "SELECT name, line, length FROM nodes WHERE path=? AND kind IN "
            "('Function','Class') ORDER BY line", (path,))]

    def disc(self, con, path, name, parent):
        sid = con.execute("SELECT id FROM nodes WHERE path=? AND name=? AND parent=?",
                          (path, name, parent)).fetchone()[0]
        return sid.split("~", 1)[1]

    def callers(self, con, sid):
        return {r[0] for r in con.execute(
            "SELECT src FROM edges WHERE dst=? AND kind='calls'", (sid,))}


class TestPythonAst(Repo):

    def test_python_uses_ast(self):
        write(self.root, "a.py", "def f():\n    return 1\n")
        con = self.index()
        self.assertEqual(self.frontend_of(con, "a.py"), "ast")

    def test_exact_spans_and_nested_definitions(self):
        write(self.root, "a.py", (
            "@decorator\n"
            "def outer(x):\n"
            "    def inner():\n"
            "        return helper()\n"
            "    return inner()\n"
            "\n"
            "\n"
            "def helper():\n"
            "    return 2\n"
        ))
        con = self.index()
        self.assertEqual(self.symbols(con, "a.py"),
                         [("outer", 2, 4), ("inner", 3, 2), ("helper", 8, 2)])
        # The call inside `inner` belongs to `inner`, not to `outer`.
        self.assertEqual(self.callers(con, "s:a.py#helper"), {"s:a.py#inner"})
        self.assertEqual(self.callers(con, "s:a.py#inner"), {"s:a.py#outer"})

    def test_code_run_by_the_enclosing_function_stays_with_it(self):
        write(self.root, "a.py", (
            "def deco(f):\n"
            "    return f\n"
            "def make():\n"
            "    return 1\n"
            "def test_thing():\n"
            "    @deco\n"
            "    def view():\n"
            "        return 2\n"
            "    class Local:\n"
            "        value = make()\n"
            "    return view\n"
        ))
        con = self.index()
        self.assertEqual(self.callers(con, "s:a.py#deco"), {"s:a.py#test_thing"})
        # The class body runs when `test_thing` defines it; the class owns it.
        self.assertEqual(self.callers(con, "s:a.py#make"), {"s:a.py#Local"})

    def test_defining_is_not_calling(self):
        # The regex harvest read `def __init__(` and `class B(A):` as calls.
        write(self.root, "a.py", (
            "class A:\n"
            "    pass\n"
            "def f():\n"
            "    class B(A):\n"
            "        def __init__(self):\n"
            "            pass\n"
            "    return B\n"
        ))
        con = self.index()
        self.assertEqual(self.callers(con, "s:a.py#__init__"), set())

    def test_scope_binds_methods_aliases_and_typed_locals(self):
        write(self.root, "store.py", (
            "class Store:\n"
            "    def save(self):\n"
            "        return 1\n"
            "class Cache:\n"
            "    def save(self):\n"
            "        return 2\n"
        ))
        write(self.root, "svc.py", (
            "from store import Store as S\n"
            "import os.path\n"
            "class Base:\n"
            "    def ping(self):\n"
            "        return 0\n"
            "class Svc(Base):\n"
            "    def __init__(self):\n"
            "        self.store = S()\n"
            "    def run(self, other: S):\n"
            "        self.ping()\n"
            "        self.store.save()\n"
            "        other.save()\n"
            "        return os.path.join('a', 'b')\n"
        ))
        con = self.index()
        # `save` exists in two classes; the name alone could not choose.
        self.assertEqual(self.callers(con, "s:store.py#save~" + self.disc(con, "store.py", "save", "Store")),
                         {"s:svc.py#run"})
        self.assertEqual(self.callers(con, "s:store.py#save~" + self.disc(con, "store.py", "save", "Cache")),
                         set())
        self.assertEqual(self.callers(con, "s:svc.py#ping"), {"s:svc.py#run"})
        self.assertEqual(self.callers(con, "s:store.py#Store"), {"s:svc.py#__init__"})
        lines = con.execute("SELECT lines FROM edges WHERE src='s:svc.py#run' "
                            "AND dst='s:svc.py#ping'").fetchone()[0]
        self.assertEqual(lines, "10")

    def test_who_calls_prints_call_sites(self):
        write(self.root, "a.py", (
            "def target():\n"
            "    return 1\n"
            "def caller():\n"
            "    target()\n"
            "    x = 2\n"
            "    return target()\n"
        ))
        _, out = run(self.index(), "who-calls", target="target")
        self.assertIn("a.py:4 caller  | target()", out)
        self.assertIn("a.py:6 caller  | return target()", out)
        self.assertIn("COMPLETE", out)

    def test_module_level_calls_belong_to_the_file(self):
        write(self.root, "deco.py", "def exempt(f):\n    return f\n")
        write(self.root, "views.py", (
            "from deco import exempt\n"
            "@exempt\n"
            "def view():\n"
            "    return 1\n"
        ))
        con = self.index()
        self.assertEqual(self.callers(con, "s:deco.py#exempt"), {"f:views.py"})
        _, out = run(con, "who-calls", target="exempt")
        self.assertIn("views.py:2 views.py", out)
        self.assertIn("COMPLETE", out)

    def test_unresolved_call_sites_are_listed_not_left_to_grep(self):
        write(self.root, "a.py", "class A:\n    def handle(self):\n        return 1\n")
        write(self.root, "b.py", "class B:\n    def handle(self):\n        return 2\n")
        write(self.root, "c.py", "def run(x):\n    return x.handle()\n")
        con = self.index()
        _, out = run(con, "who-calls", target="a.py:handle")
        self.assertNotIn("COMPLETE", out)
        self.assertIn("UNRESOLVED: 1 call site(s) named handle", out)
        self.assertIn("c.py:2 run  | return x.handle()", out)

    def test_declaration_inside_a_string_is_not_a_symbol(self):
        write(self.root, "a.py", (
            "TEMPLATE = '''\n"
            "def fake():\n"
            "    pass\n"
            "'''\n"
            "def real():\n"
            "    return TEMPLATE\n"
        ))
        con = self.index()
        self.assertEqual([s[0] for s in self.symbols(con, "a.py")], ["real"])

    def test_every_name_in_a_multi_import_is_recorded(self):
        write(self.root, "pkg/one.py", "def f():\n    return 1\n")
        write(self.root, "pkg/two.py", "def g():\n    return 2\n")
        write(self.root, "main.py", "import pkg.one, pkg.two\n")
        con = self.index()
        raws = {r[0] for r in con.execute(
            "SELECT raw FROM imports WHERE path='main.py'")}
        self.assertEqual(raws, {"pkg.one", "pkg.two"})

    def test_syntax_error_falls_back_to_regex(self):
        write(self.root, "broken.py", "def ok():\n    return (\n")
        con = self.index()
        self.assertEqual(self.frontend_of(con, "broken.py"), "regex")
        self.assertIn("ok", [s[0] for s in self.symbols(con, "broken.py")])

    def test_frontends_can_be_disabled(self):
        write(self.root, "a.py", "def f():\n    return 1\n")
        with mock.patch.dict(os.environ, {"PEACOCK_FRONTENDS": "regex"}):
            con = self.index()
        self.assertEqual(self.frontend_of(con, "a.py"), "regex")

    def test_overview_reports_the_parser(self):
        write(self.root, "a.py", "def f():\n    return 1\n")
        _, out = run(self.index(), "overview")
        self.assertIn("python:ast 1/1", out)


@unittest.skipUnless(frontends.java_available(), "no JDK on PATH")
class TestJavac(Repo):

    def test_java_uses_javac(self):
        write(self.root, "A.java", "class A { void f() {} }\n")
        con = self.index()
        self.assertEqual(self.frontend_of(con, "A.java"), "javac")

    def test_declaration_line_is_the_signature_not_the_annotation(self):
        write(self.root, "A.java", (
            "class A {\n"
            "    @Override\n"
            "    @SuppressWarnings(\"x\")\n"
            "    public <T extends Comparable<T>> java.util.List<T>\n"
            "            sorted(java.util.List<T> in,\n"
            "                   boolean desc) {\n"
            "        return helper(in);\n"
            "    }\n"
            "    java.util.List helper(java.util.List l) { return l; }\n"
            "}\n"
        ))
        con = self.index()
        syms = self.symbols(con, "A.java")
        self.assertIn(("sorted", 4, 5), syms)
        self.assertIn(("helper", 9, 1), syms)
        self.assertEqual(self.callers(con, "s:A.java#helper"), {"s:A.java#sorted"})

    def test_statements_are_never_symbols(self):
        # The shape that fabricated 25,002 regex symbols in spring-boot.
        write(self.root, "A.java", (
            "class A {\n"
            "    void run() {\n"
            "        register(bean);\n"
            "        assertThat(x);\n"
            "    }\n"
            "    void register(Object o) {}\n"
            "}\n"
        ))
        con = self.index()
        names = [s[0] for s in self.symbols(con, "A.java")]
        self.assertEqual(names, ["A", "run", "register"])

    def test_anonymous_class_methods_own_their_calls(self):
        write(self.root, "A.java", (
            "class A {\n"
            "    Runnable make() {\n"
            "        return new Runnable() {\n"
            "            public void run() { target(); }\n"
            "        };\n"
            "    }\n"
            "    static void target() {}\n"
            "}\n"
        ))
        con = self.index()
        self.assertEqual(self.callers(con, "s:A.java#target"), {"s:A.java#run"})

    def test_javac_binds_calls_to_the_right_class(self):
        # Two `handle` methods: name matching could only guess or give up.
        write(self.root, "p/A.java", "package p;\npublic class A { public void handle() {} }\n")
        write(self.root, "p/B.java", "package p;\npublic class B { public void handle() {} }\n")
        write(self.root, "p/C.java", (
            "package p;\n"
            "import java.util.function.Supplier;\n"
            "import org.missing.Lib;\n"
            "class C {\n"
            "    void run(A a, Lib lib) {\n"
            "        a.handle();\n"
            "        Runnable r = a::handle;\n"
            "        lib.handle();\n"
            "    }\n"
            "}\n"))
        con = self.index()
        a_handle = con.execute("SELECT id FROM nodes WHERE path='p/A.java' AND name='handle'").fetchone()[0]
        b_handle = con.execute("SELECT id FROM nodes WHERE path='p/B.java' AND name='handle'").fetchone()[0]
        row = con.execute("SELECT conf, lines FROM edges WHERE src='s:p/C.java#run' AND dst=?",
                          (a_handle,)).fetchone()
        self.assertEqual(tuple(row), (1.0, "6,7"))      # the call and the method reference
        # `lib.handle()` is on a type outside the repo: no guessed edge to B.
        self.assertEqual(self.callers(con, b_handle), set())

    def test_field_initializers_belong_to_the_class(self):
        write(self.root, "p/A.java", (
            "package p;\n"
            "class A {\n"
            "    static int make() { return 1; }\n"
            "    private final int x = make();\n"
            "}\n"))
        con = self.index()
        a_class = con.execute("SELECT id FROM nodes WHERE path='p/A.java' AND kind='Class'").fetchone()[0]
        make = con.execute("SELECT id FROM nodes WHERE name='make'").fetchone()[0]
        self.assertEqual(self.callers(con, make), {a_class})

    def test_parse_error_falls_back_to_regex(self):
        write(self.root, "Good.java", "class Good { void f() {} }\n")
        write(self.root, "Bad.java", "class Bad { void f() { \n")
        con = self.index()
        self.assertEqual(self.frontend_of(con, "Good.java"), "javac")
        self.assertEqual(self.frontend_of(con, "Bad.java"), "regex")

    def test_no_jdk_means_regex(self):
        write(self.root, "A.java", "class A { void f() {} }\n")
        with mock.patch.object(frontends.shutil, "which", return_value=None):
            con = self.index()
        self.assertEqual(self.frontend_of(con, "A.java"), "regex")

    def test_only_changed_files_are_sent_to_javac(self):
        write(self.root, "A.java", "class A { void f() {} }\n")
        write(self.root, "B.java", "class B { void g() {} }\n")
        self.index()
        write(self.root, "B.java", "class B { void g() {} void h() {} }\n")
        seen = []
        real = frontends.parse_java_batch

        def spy(files, *args, **kw):
            seen.extend(rel for rel, _ in files)
            return real(files, *args, **kw)

        with mock.patch.object(frontends, "parse_java_batch", spy):
            con = self.index()
        self.assertEqual(seen, ["B.java"])
        self.assertEqual(self.frontend_of(con, "A.java"), "javac")


if __name__ == "__main__":
    unittest.main()
