"""report.py — shared grouped-report rendering and exit-code conventions.

Every kicad-bench tool returns a `Result`. A tool's CLI entry point renders it and
maps it to a process exit code so the tools compose (commit-gate chains several)
and behave predictably in CI.

Exit codes (consistent across all tools):
  0  PASS  — no real problems
  1  FAIL  — at least one real problem
  2  ERROR — could not run (bad config, missing file, kicad-cli failure)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Exit(IntEnum):
    PASS = 0
    FAIL = 1
    ERROR = 2


# ANSI styling, suppressed when output is not a TTY (CI logs stay clean).
class _Style:
    def __init__(self) -> None:
        import sys
        self.on = sys.stdout.isatty()

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def bold(self, s: str) -> str:   return self._w("1", s)
    def red(self, s: str) -> str:    return self._w("31", s)
    def green(self, s: str) -> str:  return self._w("32", s)
    def yellow(self, s: str) -> str: return self._w("33", s)
    def dim(self, s: str) -> str:    return self._w("2", s)


style = _Style()


@dataclass
class Finding:
    """One reported item. `severity` drives both display and the exit code."""
    severity: str            # "error" | "warn" | "allowed" | "info" | "ok"
    message: str
    where: str = ""          # location: "U5.7", "@(120, 80)", a sheet name, ...
    detail: str = ""         # secondary line (suggested fix, reason, ...)


@dataclass
class Result:
    title: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    def add(self, severity: str, message: str, where: str = "", detail: str = "") -> None:
        self.findings.append(Finding(severity, message, where, detail))

    def error(self, msg, where="", detail=""):   self.add("error", msg, where, detail)
    def warn(self, msg, where="", detail=""):     self.add("warn", msg, where, detail)
    def allowed(self, msg, where="", detail=""):  self.add("allowed", msg, where, detail)
    def info(self, msg, where="", detail=""):     self.add("info", msg, where, detail)
    def ok(self, msg, where="", detail=""):       self.add("ok", msg, where, detail)

    @property
    def n_errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def passed(self) -> bool:
        return self.n_errors == 0

    def exit_code(self) -> int:
        return int(Exit.PASS if self.passed else Exit.FAIL)

    # -- rendering ---------------------------------------------------------
    _GLYPH = {
        "error":   ("✗", style.red),
        "warn":    ("!", style.yellow),
        "allowed": ("~", style.dim),
        "info":    ("·", style.dim),
        "ok":      ("✓", style.green),
    }

    def render(self) -> str:
        lines = [style.bold(f"== {self.title} ==")]
        for f in self.findings:
            glyph, color = self._GLYPH.get(f.severity, ("·", style.dim))
            head = f"  {color(glyph)} {f.message}"
            if f.where:
                head += style.dim(f"  [{f.where}]")
            lines.append(head)
            if f.detail:
                lines.append(style.dim(f"      {f.detail}"))
        verdict = style.green("PASS") if self.passed else style.red("FAIL")
        tail = self.summary or (
            f"{self.n_errors} error(s)" if self.n_errors else "no problems"
        )
        lines.append(f"  -> {verdict}  {style.dim(tail)}")
        return "\n".join(lines)


def render_and_exit(result: Result) -> int:
    """Print a Result and return its exit code (caller does sys.exit)."""
    print(result.render())
    return result.exit_code()
