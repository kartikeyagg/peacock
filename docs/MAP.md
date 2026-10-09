# `peacock map` — scorecard and code map

`peacock q` is for agents. `peacock map` is for the people working alongside
them: it scores how easy a repository is for an AI agent to work in, and draws
the repository as an interactive map in your browser.

```bash
PEACOCK="python3 skills/peacock/scripts/peacock.py"
$PEACOCK map /path/to/repository
```

This builds (or incrementally updates) the index, prints a scorecard, writes a
self-contained report to `<repo>/.peacock/report/`, and serves it at
`http://127.0.0.1:7278/` (the next free port if that one is taken). Press
`Ctrl+C` to stop the server.

The map is drawn from the same index `peacock q` reads, so a call edge on the
map is the same call edge `who-calls` reports — compiler-resolved for Python
and Java where available, inferred elsewhere.

## Options

| Option | Effect |
|---|---|
| `--view nebula` / `--view atlas` | Initial view: 3D graph (default) or flat 2D map |
| `--json` | Print `meta`, `stats` and `scores` as JSON and exit — no report, no server |
| `--build-only` | Write the report and exit without serving it |
| `--no-open` | Serve without launching a browser |
| `--out DIR` | Write the report to `DIR` instead of `.peacock/report/` |
| `--port N` | Start looking for a free port at `N` (default 7278) |
| `--max-nodes N` | Cap drawn nodes (default 2500); modules, files and libraries are always kept, then the most-connected classes and functions |
| `--all` | Include files ignored by Git |

`--build-only` output is static: open `index.html` from any local web server
(browsers block `data.json` over `file://`).

## The scorecard

Five metrics, each 0–100, higher meaning friendlier to an agent. The overall
score is a weighted mean, graded A+ (≥92) to F (<45).

| Metric | Weight | Asks |
|---|---:|---|
| AI Navigability | 22% | Can an agent find things fast? File size, naming clarity, directory shape, a README/entry point |
| Context Efficiency | 22% | How little must it load to work? Function length, symbols per file, duplication, file size |
| Modularity & Coupling | 20% | Are boundaries clean? Import fan-out, import cycles, hub files, external dependency count |
| Complexity & Maintainability | 18% | How tangled is the logic? Branching and nesting |
| Self-Description | 18% | Do docs and types explain intent for free? Docstrings, type hints, README |

Each metric also produces short notes; the top five are printed as
"suggestions for AI-readiness". The scores are opinionated heuristics for
agent ergonomics, not a style linter and not a quality guarantee.

## The views

Press `v` (or the 🌌 / 🗺 buttons) to switch.

- **Nebula** — a 3D force layout. Drag to orbit, scroll to zoom, right-drag to
  pan. Colour is node type, size is connectedness.
- **Atlas** — a flat map. Each directory is a labelled rectangle containing
  its files; cross-directory imports collapse into one weighted route per
  pair. Click a file to fan out its functions and see its exact edges.

In either view: hover to name a node, click to inspect it and highlight its
neighbourhood, and use the search box to fly to a symbol. **Extend live** in
the right panel grafts a hypothetical import or library onto the graph so you
can see where it would land (this changes only the page, not your code).

## Open in VS Code

If a VS Code-compatible CLI (`code`, `code-insiders`, `codium` or `cursor`) is
on `PATH`, select a node and press `o` to jump to that file and line in your
editor. Peacock offers to open the project on launch.

The server listens on `127.0.0.1` only, and the editor bridge refuses any path
that resolves outside the repository being mapped.
