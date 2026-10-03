"""
Output formatting under a token budget.

Every byte a query emits lands in an agent's context window, so this module has
exactly one job: say the most per token and stop cleanly when the budget runs
out.

Two rules drive the format:

  * **Lines, not JSON.** `engine/graph.py:56 build_graph c14 L58 <-2 ->7` costs
    about 18 tokens. The same facts as a JSON object cost roughly 60, and
    two-thirds of that is punctuation and repeated key names. JSON is still
    available behind `--json` for programmatic consumers.
  * **Truncate loudly.** A silently-cut list is a correctness bug: the agent
    concludes "only 12 callers" when there were 300. Every truncation states
    what was dropped and how to narrow the query.
"""
from __future__ import annotations

# Rough bytes-per-token for the mixed code/path text these queries emit. Real
# tokenizers vary; this is deliberately conservative so we under-fill rather
# than blow a caller's budget.
CHARS_PER_TOKEN = 4

DEFAULT_BUDGET = 2000


def est_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


class Budget:
    """Accumulates output lines and refuses them once the budget is spent."""

    def __init__(self, tokens=DEFAULT_BUDGET):
        self.limit = max(64, int(tokens))
        self.used = 0
        self.lines = []
        self.dropped = 0
        self._full = False

    @property
    def full(self):
        return self._full

    def add(self, line=""):
        """Append a line. Returns False once the budget is exhausted."""
        if self._full:
            self.dropped += 1
            return False
        cost = est_tokens(line) + 1
        # Reserve a little headroom so the truncation notice always fits.
        if self.used + cost > self.limit - 12:
            self._full = True
            self.dropped += 1
            return False
        self.lines.append(line)
        self.used += cost
        return True

    def extend(self, lines):
        for l in lines:
            if not self.add(l):
                return False
        return True

    def note(self, hint=""):
        """Close out with an honest statement of what was cut."""
        if not self._full and not self.dropped:
            return
        msg = f"... +{self.dropped} line(s) cut (token budget reached)"
        if hint:
            msg += f" -- {hint}"
        self.lines.append(msg)

    def render(self):
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- #
#  Node rendering                                                              #
# --------------------------------------------------------------------------- #
def loc_of(node):
    """`path:line` for symbols, plain `path` for files/dirs."""
    path = node["path"] or node["name"]
    line = node["line"] if "line" in node.keys() else None
    return f"{path}:{line}" if line else path


def sym_line(node, show_deg=True):
    """One symbol as one line: where it is, how big, how connected."""
    bits = [loc_of(node), node["name"]]
    kind = node["kind"]
    if kind == "Class":
        bits.append("[class]")
    elif kind == "Library":
        bits.append("[lib]")
    cx = node["complexity"] if "complexity" in node.keys() else None
    ln = node["length"] if "length" in node.keys() else None
    if cx:
        bits.append(f"c{cx}")
    if ln:
        bits.append(f"L{ln}")
    if show_deg:
        ind = node["indeg"] if "indeg" in node.keys() else 0
        outd = node["outdeg"] if "outdeg" in node.keys() else 0
        bits.append(f"<-{ind or 0} ->{outd or 0}")
    return " ".join(str(b) for b in bits)


def file_line(node, extra=""):
    bits = [node["path"] or node["name"]]
    if "lang" in node.keys() and node["lang"]:
        bits.append(node["lang"])
    if "length" in node.keys() and node["length"]:
        bits.append(f"{node['length']}L")
    ind = node["indeg"] if "indeg" in node.keys() else 0
    outd = node["outdeg"] if "outdeg" in node.keys() else 0
    bits.append(f"<-{ind or 0} ->{outd or 0}")
    if extra:
        bits.append(extra)
    return " ".join(str(b) for b in bits)


def conf_tag(conf):
    """Confidence marker on a heuristic edge.

    Call edges are inferred, not proven. Showing the tier lets an agent decide
    whether to trust the answer or go read the source — silently presenting a
    0.6-confidence guess as fact is how a graph misleads.
    """
    if conf is None or conf >= 0.95:
        return ""
    if conf >= 0.8:
        return " ~"        # resolved via an import
    return " ?"            # unique-name guess


def shown(n, total, noun):
    """`(3 callers)` when complete, `(showing 200 of 292 callers)` when not.

    The budget can only report lines it was offered. Anything capped earlier —
    by a SQL LIMIT, by a visited-node ceiling — has to announce itself here, or
    an agent reads a floor as an exact count and acts on it.
    """
    if total is None or total <= n:
        return f"({n} {_plural(noun, n)})"
    return f"(showing {n} of {total} {_plural(noun, total)} -- INCOMPLETE)"


def _plural(noun, n):
    if n == 1:
        return noun
    return noun + ("es" if noun.endswith(("s", "x", "ch", "sh")) else "s")


def listing(items, limit):
    """Join a list, stating the remainder rather than dropping it quietly."""
    head = " ".join(str(i) for i in items[:limit])
    extra = len(items) - limit
    return head + (f"  ...+{extra} more" if extra > 0 else "")


def cap_warning(capped):
    return "  [TRAVERSAL CAPPED -- result is a floor, not a total]" if capped else ""


def bar(score, width=20):
    filled = int(round(max(0.0, min(100.0, score)) / 100 * width))
    return "#" * filled + "." * (width - filled)
