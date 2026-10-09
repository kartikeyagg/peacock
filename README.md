<div align="center">

# 🦚 Peacock

### Compact codebase intelligence for Codex and Claude

Peacock builds a local graph of files, classes, functions, imports, and calls.
Agents query that graph to orient themselves and load only the source relevant
to a task instead of repeatedly searching and reading whole files.  The Aim of this project is to ease the AI driven  development . More features are on the way .

</div>

## Why use it

```text
overview           repository map and call-graph coverageb 
find X             definitions named X
outline file       symbols, imports, and importers
who-calls X        direct callers with source locations
impact X           reverse-reachable blast radius
subgraph A B       compact task-specific code slice
span X             exact source for one symbol
map                AI-readiness scorecard + interactive 3D/2D code map
```

Answers are line-oriented and token-budgeted. Truncation, parser uncertainty,
and inferred call edges are reported rather than silently hidden.

Everything runs locally using Python's standard library. There is no hosted
service, account, API key, telemetry, or repository upload.

See [REQUIREMENTS.md](REQUIREMENTS.md) for Linux, macOS, and Windows setup,
custom `PATH`/`JAVA_HOME` configuration, and troubleshooting when Python or the
JDK is installed but not visible to Codex or Claude.

## Install for Codex

Ask Codex's built-in skill installer:

```text
$skill-installer
Install the peacock skill from https://github.com/kartikeyagg/peacock/tree/main/skills/peacock
```

Restart Codex if the skill does not appear immediately. Invoke it explicitly
with `$peacock`, or let Codex select it for repository navigation and impact
analysis.

The repository also includes a Codex plugin manifest for plugin-directory
distribution.

## Install for Claude Code

Add this repository as a marketplace, then install the plugin:

```bash
claude plugin marketplace add kartikeyagg/peacock
claude plugin install peacock@peacock
```

Start a new session or reload plugins. Invoke the skill as
`/peacock:peacock`, or let Claude select it automatically.

For a one-off local checkout:

```bash
claude --plugin-dir /path/to/peacock
```

> **Windows:** the plugin starts its MCP server with `python3`, which on most
> Windows machines is a Microsoft Store placeholder rather than Python, so the
> server fails with `Connection closed`. See [Windows setup](#windows-setup).

## Use the bundled CLI

The skill uses this interface when MCP tools are unavailable:

```bash
PEACOCK="python3 skills/peacock/scripts/peacock.py"
$PEACOCK index /path/to/repository
cd /path/to/repository
$PEACOCK q overview
$PEACOCK q find parse
$PEACOCK q who-calls parse
$PEACOCK q subgraph parser index
```

Re-run `index` after changing source. Indexing is incremental.

## See the map

`peacock map` is for humans: it scores a repository's AI-readiness out of 100
and opens an interactive map of its files, classes, functions, imports and
calls in your browser.

```bash
$PEACOCK map /path/to/repository             # scorecard + live map
$PEACOCK map /path/to/repository --view atlas # start in the flat 2D view
$PEACOCK map /path/to/repository --json       # scorecard only, as JSON
```

The map is drawn from the same index `peacock q` answers from, is served on
`127.0.0.1` only, and needs no network. See [docs/MAP.md](docs/MAP.md) for the
five metrics, the views, keyboard shortcuts and the VS Code bridge.

## Windows setup

On Windows, `python3` usually is not a real interpreter, and the commands above
use Bash syntax. In PowerShell, use a local checkout whose MCP server is started
with the Python launcher (`py -3`) instead:

```powershell
# 1. Get a local copy
$Peacock = "$HOME\peacock"
git clone https://github.com/kartikeyagg/peacock.git $Peacock

# 2. Start the MCP server with `py -3` instead of `python3`
foreach ($f in "$Peacock\.claude-plugin\plugin.json", "$Peacock\.mcp.json") {
    (Get-Content $f -Raw) -replace '"command": "python3",\s*"args": \[', '"command": "py", "args": ["-3", ' |
        Set-Content $f
}

# 3. Start Claude Code with the plugin
claude --plugin-dir $Peacock
```

Without git, replace step 1 with a zip download:

```powershell
$Peacock = "$HOME\peacock"
Invoke-WebRequest https://github.com/kartikeyagg/peacock/archive/refs/heads/main.zip -OutFile "$env:TEMP\peacock.zip"
Expand-Archive "$env:TEMP\peacock.zip" -DestinationPath "$env:TEMP\peacock-zip" -Force
Move-Item "$env:TEMP\peacock-zip\peacock-main" $Peacock
```

If `py` is not installed but `python` works, use `'"command": "python", "args": ['`
as the replacement in step 2.

The CLI in PowerShell:

```powershell
$PeacockCli = "$Peacock\skills\peacock\scripts\peacock.py"
py -3 $PeacockCli index C:\path\to\repository
Set-Location C:\path\to\repository
py -3 $PeacockCli q overview
py -3 $PeacockCli q find parse
```

See [REQUIREMENTS.md](REQUIREMENTS.md#windows) for installing Python and the
JDK, and for `PATH` troubleshooting.

## Optional MCP server

The Codex and Claude plugin manifests start the bundled stdio MCP server. To
connect another MCP client, configure:

```json
{
  "command": "python3",
  "args": [
    "/absolute/path/to/peacock/skills/peacock/scripts/peacock.py",
    "mcp",
    "--repo",
    "/absolute/path/to/your/repository"
  ]
}
```

The server exposes `index`, `overview`, `find`, `outline`, `who_calls`,
`calls`, `neighbors`, `deps`, `dependents`, `impact`, `subgraph`, `span`,
`hubs`, and `cycles`.

## Accuracy boundaries

Peacock indexes files, classes, functions, imports, and calls. It does not
index constants, fields, variables, configuration keys, or string contents;
use exact text search for those.

Python uses the standard-library AST. Java uses `javac` when a JDK is present.
Other supported languages use a conservative regex parser. Call resolution is
static and can miss dynamic dispatch, reflection, and dependency injection.
Every overview reports coverage, and inferred edges are marked.

## Development

```bash
python3 -m unittest discover -s skills/peacock/scripts/tests -p 'test_*.py'
python3 skills/peacock/scripts/peacock.py index .
python3 skills/peacock/scripts/peacock.py q overview
python3 skills/peacock/scripts/peacock.py map . --build-only
```

Python 3.9 or newer is required. JDK 17 or newer is optional and improves Java
results. Full platform setup is documented in
[REQUIREMENTS.md](REQUIREMENTS.md).

## Public and private boundary

This repository contains the agent-facing parser, index, compact query engine,
skill, MCP adapter, and the `map` scorecard and visualization. Research
write-ups and experimental analyses are maintained separately.

## License

MIT
