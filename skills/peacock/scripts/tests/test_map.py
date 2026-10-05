"""
Tests for `peacock map`: the scorecard, the browser report, and its server.

    python3 -m unittest discover -s tests -p "test_map.py" -v

The map is drawn from the agent index, so these check that the picture agrees
with the index, that the report is self-contained, and that the local server
cannot be talked into opening files outside the repository.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import peacock                                                    # noqa: E402
import peacock_map                                                # noqa: E402
from engine.graph import from_index                               # noqa: E402
from engine.index import build_index, connect                     # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class MapFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peacock-map-")
        write(self.root, "README.md", "# demo\n")
        write(self.root, "app/main.py", (
            "from app.helpers import compute\n"
            "\n"
            "def run():\n"
            "    '''Entry point.'''\n"
            "    return compute(3)\n"
        ))
        write(self.root, "app/helpers.py", (
            "def compute(n: int) -> int:\n"
            "    return double(n)\n"
            "\n"
            "def double(n):\n"
            "    return n * 2\n"
            "\n"
            "class Store:\n"
            "    def get(self):\n"
            "        return compute(1)\n"
        ))
        write(self.root, "web/ui.js", (
            "export function render(x) {\n"
            "  return x + 1;\n"
            "}\n"
        ))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def analyze(self, **kw):
        with redirect_stderr(io.StringIO()):
            return peacock_map.analyze(self.root, **kw)


class TestGraphFromIndex(MapFixture):
    def test_graph_matches_index(self):
        st = build_index(self.root, log=lambda *_: None)
        con = connect(st["db"])
        try:
            files, graph = from_index(con)
        finally:
            con.close()
        paths = {f.path for f in files}
        self.assertTrue({"app/main.py", "app/helpers.py", "web/ui.js"} <= paths)
        labels = {n["label"] for n in graph.nodes.values()}
        self.assertTrue({"run", "compute", "double", "Store"} <= labels)
        ids = set(graph.nodes)
        for e in graph.edges:
            self.assertIn(e["source"], ids)
            self.assertIn(e["target"], ids)
        self.assertIn("calls", {e["kind"] for e in graph.edges})


class TestAnalyze(MapFixture):
    def test_scores_and_stats(self):
        data = self.analyze()
        scores = data["scores"]
        self.assertTrue(0 <= scores["overall"] <= 100)
        self.assertTrue(scores["grade"])
        for key in peacock_map.METRIC_NAMES:
            self.assertTrue(0 <= scores["metrics"][key]["score"] <= 100, key)
            self.assertIsInstance(scores["metrics"][key]["notes"], list)
        st = data["stats"]
        self.assertEqual(st["code_files"], 3)
        self.assertEqual(st["classes"], 1)
        self.assertIn("python", st["languages"])

    def test_every_node_is_laid_out_in_both_views(self):
        data = self.analyze(view="atlas")
        self.assertEqual(data["meta"]["view"], "atlas")
        nodes = data["graph"]["nodes"]
        self.assertTrue(nodes)
        for n in nodes:
            for k in ("x", "y", "z", "ax", "ay"):
                self.assertIsInstance(n[k], (int, float), (n["id"], k))
        self.assertIn("cells", data["graph"]["atlas"])

    def test_prune_keeps_structure_and_drops_dangling_edges(self):
        data = self.analyze(max_nodes=1)
        kinds = {n["kind"] for n in data["graph"]["nodes"]}
        self.assertIn("File", kinds)
        self.assertNotIn("Function", kinds)
        ids = {n["id"] for n in data["graph"]["nodes"]}
        for e in data["graph"]["edges"]:
            self.assertIn(e["source"], ids)
            self.assertIn(e["target"], ids)

    def test_not_a_directory(self):
        with self.assertRaises(SystemExit):
            self.analyze_missing()

    def analyze_missing(self):
        with redirect_stderr(io.StringIO()):
            peacock_map.analyze(os.path.join(self.root, "nope"))


class TestCli(MapFixture):
    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = peacock.main(["map", self.root, *argv])
        return code, out.getvalue()

    def test_json_summary(self):
        code, out = self.run_cli("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"meta", "stats", "scores"})

    def test_build_only_writes_self_contained_report(self):
        code, out = self.run_cli("--build-only")
        self.assertEqual(code, 0)
        self.assertIn("SCORECARD", out)
        report = os.path.join(self.root, ".peacock", "report")
        for rel in ("index.html", "app.js", "style.css", "data.json",
                    "vendor/three.min.js", "vendor/OrbitControls.js"):
            self.assertTrue(os.path.isfile(os.path.join(report, rel)), rel)
        with open(os.path.join(report, "data.json"), encoding="utf-8") as fh:
            self.assertIn("graph", json.load(fh))

    def test_report_is_not_indexed(self):
        self.run_cli("--build-only")
        st = build_index(self.root, log=lambda *_: None)
        con = connect(st["db"])
        try:
            paths = [r[0] for r in con.execute("SELECT path FROM files")]
        finally:
            con.close()
        self.assertFalse([p for p in paths if p.startswith(".peacock")])

    def test_custom_out_dir(self):
        out_dir = os.path.join(self.root, "elsewhere")
        code, _ = self.run_cli("--build-only", "--out", out_dir)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "data.json")))


class TestServer(MapFixture):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.root, ".peacock", "report")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            peacock.main(["map", self.root, "--build-only"])
        self.httpd = peacock_map.bind(self.out, 0, project_root=self.root)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.read()

    def test_serves_report(self):
        self.assertIn(b"<html", self.get("/index.html").lower())
        self.assertIn("graph", json.loads(self.get("/data.json")))

    def test_binds_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_open_refuses_paths_outside_repo(self):
        launched = []
        with mock.patch.object(peacock_map, "find_vscode", return_value=["code"]), \
                mock.patch("subprocess.Popen", side_effect=launched.append):
            outside = json.loads(self.get("/api/open?path=../../etc/passwd&line=1"))
            inside = json.loads(self.get("/api/open?path=app/main.py&line=3"))
        self.assertFalse(outside["ok"])
        self.assertTrue(inside["ok"])
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0][-1].endswith("main.py:3"))

    def test_open_without_editor(self):
        with mock.patch.object(peacock_map, "find_vscode", return_value=None):
            status = json.loads(self.get("/api/vscode-status"))
            opened = json.loads(self.get("/api/open?path=app/main.py"))
        self.assertFalse(status["available"])
        self.assertFalse(opened["ok"])


if __name__ == "__main__":
    unittest.main()
