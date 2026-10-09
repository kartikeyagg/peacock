# Requirements and cross-platform setup

Peacock runs entirely on the user's computer. It does not upload repository
contents and does not require an API key or a hosted Peacock service.

## Requirements at a glance

| Component | Required? | Supported version | Purpose |
|---|---:|---|---|
| Python | Yes | Python 3.9 or newer | Runs the skill, indexer, query engine, and MCP server |
| Python packages | No | None | Peacock uses only the Python standard library |
| JDK | No | JDK 17 or newer recommended | Gives Java compiler-grade symbols and call resolution |
| Git | Installation only | Any maintained version | Clones or updates the public repository |
| Write access | Yes | — | Creates `.peacock/index.db` inside the repository being indexed |
| Web browser | `map` only | Any with WebGL | Displays the `peacock map` view; no internet access needed |

Python includes the `ast` compiler frontend Peacock uses for Python source.
There is no separate Python compiler to install.

The JDK is optional. Without it, Peacock still indexes Java using its fallback
parser. A JRE is not enough for compiler-backed Java analysis: both `java` and
`javac` must be available.

## Check the environment first

Peacock must be launched by a Python interpreter, and optional Java compiler
support is enabled only when both Java commands can be found.

Linux or macOS:

```bash
python3 --version
command -v python3
java --version
javac --version
command -v java
command -v javac
```

Windows PowerShell:

```powershell
py -3 --version
python --version
Get-Command py -ErrorAction SilentlyContinue
Get-Command python -ErrorAction SilentlyContinue
java --version
javac --version
Get-Command java -ErrorAction SilentlyContinue
Get-Command javac -ErrorAction SilentlyContinue
```

Only one working Python command is needed. On Windows this is commonly
`py -3` or `python`, not `python3`.

## Linux

### Install prerequisites

Debian or Ubuntu:

```bash
sudo apt update
sudo apt install python3 git
sudo apt install openjdk-17-jdk    # optional Java frontend
```

Fedora:

```bash
sudo dnf install python3 git
sudo dnf install java-17-openjdk-devel    # optional Java frontend
```

Arch Linux:

```bash
sudo pacman -S python git
sudo pacman -S jdk17-openjdk    # optional Java frontend
```

### Add a custom installation to `PATH`

If Python or the JDK was installed outside the package manager, add its `bin`
directory to the shell startup file. Use `~/.bashrc` for Bash or `~/.zshrc`
for Zsh:

```bash
export PATH="/opt/python/bin:$PATH"
export JAVA_HOME="/opt/jdk-17"
export PATH="$JAVA_HOME/bin:$PATH"
```

Reload the file and verify both commands:

```bash
source ~/.bashrc    # use ~/.zshrc when appropriate
python3 --version
javac --version
```

Then restart Codex or Claude so the application receives the new environment.

## macOS

Recent macOS installations do not guarantee a usable Python interpreter.
Install one explicitly rather than relying on an Apple system component.

Using Homebrew:

```bash
brew install python
brew install openjdk@17    # optional Java frontend
```

Homebrew normally configures Python on `PATH`. For an Apple Silicon Homebrew
installation, these lines in `~/.zshrc` cover the common paths:

```bash
export PATH="/opt/homebrew/bin:$PATH"
export PATH="/opt/homebrew/opt/openjdk@17/bin:$PATH"
export JAVA_HOME="/opt/homebrew/opt/openjdk@17"
```

For an Intel Homebrew installation, replace `/opt/homebrew` with
`/usr/local`.

If the JDK was installed as a macOS Java bundle, ask macOS for its location:

```bash
/usr/libexec/java_home -V
export JAVA_HOME="$(/usr/libexec/java_home -v 17)"
export PATH="$JAVA_HOME/bin:$PATH"
```

Reload and verify:

```bash
source ~/.zshrc
python3 --version
java --version
javac --version
```

Applications opened from Finder or the Dock may not receive changes made only
in a shell startup file. After changing `PATH`, fully quit and reopen the app,
or launch Codex/Claude from the verified terminal.

## Windows

### Install prerequisites

Install Python 3.9 or newer from python.org, the Microsoft Store, `winget`, or
your organization's software manager. When using the python.org installer,
enable **Add Python to PATH**.

Example with `winget`:

```powershell
winget install Python.Python.3.12
winget install Git.Git
winget install EclipseAdoptium.Temurin.17.JDK    # optional Java frontend
```

Close and reopen PowerShell after installation, then run the verification
commands above.

### Add Python or Java to `PATH`

1. Open **System Properties → Advanced → Environment Variables**.
2. Under the current user's variables, edit `Path`.
3. Add the directory containing `python.exe`. A common location is:
   `C:\Users\<you>\AppData\Local\Programs\Python\Python312`.
4. For Java, create `JAVA_HOME` pointing at the JDK directory, not its `bin`
   directory. A common location is:
   `C:\Program Files\Eclipse Adoptium\jdk-17...`.
5. Add `%JAVA_HOME%\bin` to `Path`.
6. Fully restart the terminal and Codex/Claude.

Verify which executable Windows will use:

```powershell
where.exe python
where.exe java
where.exe javac
python --version
java --version
javac --version
```

Windows may expose a Microsoft Store execution alias named `python.exe` even
when it is not the intended interpreter. If `python` opens the Store, use
`py -3`, put the real Python directory earlier on `Path`, or disable the alias
under **Settings → Apps → Advanced app settings → App execution aliases**.

## Install and run Peacock

### Codex skill

Ask Codex:

```text
$skill-installer
Install the peacock skill from https://github.com/kartikeyagg/peacock/tree/main/skills/peacock
```

After installation, invoke `$peacock` or allow Codex to select it when a task
needs repository orientation, caller tracing, dependency analysis, or a small
source slice.

### Claude Code plugin

```bash
claude plugin marketplace add kartikeyagg/peacock
claude plugin install peacock@peacock
```

Start a new Claude session or run `/reload-plugins` after installation.

### Run the bundled CLI directly

Linux or macOS:

```bash
python3 skills/peacock/scripts/peacock.py index /path/to/repository
cd /path/to/repository
python3 /path/to/peacock/skills/peacock/scripts/peacock.py q overview
```

Windows PowerShell with the Python launcher:

```powershell
py -3 .\skills\peacock\scripts\peacock.py index C:\path\to\repository
Set-Location C:\path\to\repository
py -3 C:\path\to\peacock\skills\peacock\scripts\peacock.py q overview
```

Replace `py -3` with `python` or an absolute path to `python.exe` when that is
the interpreter available on the machine.

## Configure an MCP client with an absolute interpreter path

An absolute interpreter path is the most reliable solution when a graphical
application has a different `PATH` from the terminal.

Linux example:

```json
{
  "command": "/usr/bin/python3",
  "args": [
    "/home/you/peacock/skills/peacock/scripts/peacock.py",
    "mcp",
    "--repo",
    "/home/you/project"
  ]
}
```

macOS Apple Silicon example:

```json
{
  "command": "/opt/homebrew/bin/python3",
  "args": [
    "/Users/you/peacock/skills/peacock/scripts/peacock.py",
    "mcp",
    "--repo",
    "/Users/you/project"
  ]
}
```

Windows example using the Python launcher:

```json
{
  "command": "py",
  "args": [
    "-3",
    "C:\\Users\\you\\peacock\\skills\\peacock\\scripts\\peacock.py",
    "mcp",
    "--repo",
    "C:\\Users\\you\\project"
  ]
}
```

Alternatively, set `command` to the full escaped path to `python.exe` and
remove `"-3"` from `args`.

The distributed plugin manifests use `python3` by default. On a system where
that command does not exist—most notably many Windows installations—either
make `python3` available on `PATH`, use the skill through `py -3`, or clone the
plugin and change the manifest's `command` to the working interpreter before
loading it with `claude --plugin-dir C:\path\to\peacock`.

## Installed, but Peacock cannot find Python

Check these in order:

1. Run the version command in the same terminal that starts Codex or Claude.
2. Use `command -v python3` on Linux/macOS or `Get-Command python` and
   `Get-Command py` on Windows.
3. Fully restart the application after changing `PATH`.
4. If the app was opened from a desktop icon, launch it once from the terminal
   where Python works.
5. Use the absolute interpreter path in the MCP configuration.
6. On Windows, check for the Microsoft Store execution alias and use `py -3`
   when available.

If no Python command works, Peacock cannot run. Install Python 3.9 or newer;
the Markdown skill alone cannot generate an index.

## Installed, but Peacock cannot find the JDK

Peacock checks both `java` and `javac` using `PATH`. It does not currently use
`JAVA_HOME` by itself. Therefore `JAVA_HOME` must be accompanied by its `bin`
directory on `PATH`.

Check these cases:

- `java` exists but `javac` does not: a JRE is installed, not a full JDK.
- Both commands exist in a terminal but not the app: restart the app or launch
  it from that terminal.
- `JAVA_HOME` points to a JDK but `%JAVA_HOME%\bin` or `$JAVA_HOME/bin` is not
  on `PATH`.
- Several JDKs are installed: `where.exe java` on Windows or `command -v java`
  on Linux/macOS shows which one wins.
- The JDK is older than 17: the bundled Java helper uses modern Java syntax and
  may fail to launch. Select JDK 17 or newer.

Missing or unusable Java tooling is non-fatal. Peacock logs that the compiler
frontend is unavailable and falls back to its Java parser. Run `q overview`
and inspect the `PARSERS` line to see whether Java used `javac` or `regex`.

To intentionally disable compiler frontends for troubleshooting:

Linux or macOS:

```bash
PEACOCK_FRONTENDS=regex python3 skills/peacock/scripts/peacock.py index . --force
```

Windows PowerShell:

```powershell
$env:PEACOCK_FRONTENDS = "regex"
py -3 .\skills\peacock\scripts\peacock.py index . --force
```

## Files Peacock creates

Peacock writes `.peacock/index.db` inside the indexed repository, and
`peacock map` writes its report to `.peacock/report/` unless `--out` is given. The directory
is safe to delete because it is regenerated from source, but it should normally
be added to `.gitignore` and left in place for fast incremental re-indexing.
