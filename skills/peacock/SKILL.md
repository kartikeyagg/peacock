---
name: peacock
description: Navigate and change unfamiliar codebases through a precomputed file, symbol, import, and call graph. Use for repository orientation, locating definitions, tracing callers or dependencies, estimating blast radius, selecting the smallest relevant source spans, and avoiding broad file reads. Do not use it for constants, fields, configuration keys, or exact text searches.
---

# Peacock

Use Peacock to answer structural questions with a small graph query before
reading source files. It indexes files, classes, functions, imports, and calls
locally; repository contents never leave the machine.

## Choose the interface

If Peacock MCP tools are connected, use them directly. Call `index` with the
repository root before the first query and after edits.

Otherwise, resolve the directory containing this `SKILL.md` and run the
bundled dependency-free CLI. Use `python3` on Linux/macOS; on Windows use the
first working choice among `py -3`, `python`, and `python3`:

```bash
python3 "<skill-dir>/scripts/peacock.py" index .
python3 "<skill-dir>/scripts/peacock.py" q overview
```

Keep using the same runner path for the commands below.

## Workflow

1. Run `overview` once for repository shape and call-graph coverage.
2. Use `find` or `subgraph` to locate the relevant region.
3. Use `outline` on candidate files instead of opening them wholesale.
4. Use `span` for only the symbols needed for the task.
5. Open a complete file when editing it or when the selected spans are
   insufficient.
6. Re-index after changing source code.

## Query selection

| Question | Query |
|---|---|
| What is this repository? | `q overview` |
| Where does a symbol live? | `q find <name>` |
| What is declared in a file? | `q outline <file>` |
| Who calls a function? | `q who-calls <symbol>` |
| What does it call? | `q calls <symbol>` |
| What can a change affect? | `q impact <symbol>` |
| What is the smallest relevant slice? | `q subgraph <seed>...` |
| Show one symbol's source | `q span <symbol>` |
| What does a file import? | `q deps <file> [--transitive]` |
| Who imports a file? | `q dependents <file> [--transitive]` |
| Where is coupling concentrated? | `q hubs` or `q cycles` |

Use `path:name` to disambiguate repeated names, for example:

```bash
python3 "<skill-dir>/scripts/peacock.py" q who-calls engine/index.py:_resolve_import
```

## Interpret results conservatively

- `path:line name c<complexity> L<lines> <-<callers> -><callees>` is the
  compact symbol format.
- `~` on a call edge means it was resolved through an import.
- `?` means a unique-name inference; verify it against a span or exact search.
- `INCOMPLETE`, `TRAVERSAL CAPPED`, or `budget reached` means the answer is
  partial. Narrow the query or raise `--budget`.
- Below roughly 80% call-graph coverage, caller results are a lower bound.
  Confirm with exact text search before declaring code unused or deleting it.
- A `PARSER` warning means symbol lists may omit declarations and inferred
  calls elsewhere may be misattributed.

Peacock does not index constants, fields, variables, configuration keys, or
string contents. Use exact text search for those and to prove that a reference
does not exist.

## Scorecard and map for humans

When the user asks how agent-friendly a repository is, or wants to see its
structure, run `map`. It is not for agent navigation: it starts a local server
and opens a browser.

```bash
python3 "<skill-dir>/scripts/peacock.py" map . --json   # scorecard only, safe in a terminal
python3 "<skill-dir>/scripts/peacock.py" map .          # scorecard + interactive map (blocks; Ctrl+C stops it)
```

Report the overall score, the five metric scores, and the listed suggestions.
