"""review — datasheet-grounded per-IC schematic review (`kb review`).

The semantic layer above ERC: for each IC, a Claude reviewer gets the design
graph, the extracted constraints, and budgeted access to the ingested datasheet
pages (text or rendered image), and files severity-calibrated findings cited to
pages. Ideas from Pinscope's reviewer (original implementation): per-IC
isolation, feasibility-not-direction, "error requires a concrete harm pathway",
and a code-enforced **downgrade-only** normalization pass — the model can never
talk a finding *up*, only the evidence can.

Outputs:
  * findings   -> committed `.cockpit/review.json` (page-cited review artifact)
  * usage log  -> gitignored `.audit-cache/review-usage.jsonl` (tokens per call)

Needs the optional `llm` extra (`pip install 'kicad-bench[llm]'`) and
ANTHROPIC_API_KEY. Exit 0/1/2 per kb convention (post-normalization errors fail).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import datasheet as dsmod
from .core import config as cfgmod, conspec, graph as graphmod
from .core.report import Result, render_and_exit
from .core.review_prompt import SYSTEM_PROMPT

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MIN_PINS = 8
DEFAULT_PAGE_BUDGET = 24        # total datasheet pages per IC review
DEFAULT_NEIGHBOR_BUDGET = 8     # pages per *neighbor* datasheet within that
MAX_TURNS = 40                  # hard cap on the tool loop
IMAGE_DPI = 150

_SEVERITIES = ("error", "warning", "info")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BACKEND = "claude-code"      # subscription auth via the claude CLI; "api" opt-in
CLAUDE_TIMEOUT = 1800                # seconds per IC (headless runs can take minutes)
# Builtin Claude Code tools the reviewer must not use — everything goes through
# the kbreview MCP tools so page budgets and cited-page tracking stay enforced.
_CLAUDE_DISALLOWED = ("Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,"
                      "NotebookEdit,Task,TodoWrite")


def review_model(cfg: cfgmod.Config) -> str | None:
    """Configured model override, or None (run_review_auto falls back to DEFAULT_MODEL)."""
    raw = cfg.raw.get("llm", {}) if isinstance(cfg.raw, dict) else {}
    return os.environ.get("KB_REVIEW_MODEL") or raw.get("review_model")


def review_backend(cfg: cfgmod.Config) -> str:
    raw = cfg.raw.get("llm", {}) if isinstance(cfg.raw, dict) else {}
    return (os.environ.get("KB_REVIEW_BACKEND") or raw.get("backend")
            or DEFAULT_BACKEND)


def _review_cfg(cfg: cfgmod.Config) -> dict:
    raw = cfg.raw.get("review", {}) if isinstance(cfg.raw, dict) else {}
    return {
        "min_pins": int(raw.get("min_pins", DEFAULT_MIN_PINS)),
        "page_budget": int(raw.get("page_budget", DEFAULT_PAGE_BUDGET)),
        "neighbor_budget": int(raw.get("neighbor_budget", DEFAULT_NEIGHBOR_BUDGET)),
    }


# ---------------------------------------------------------------------------
# Datasheet access (slug -> index dir), page text / image
# ---------------------------------------------------------------------------

class DatasheetLibrary:
    """Slug-addressed access to every ingested datasheet across index roots."""

    def __init__(self, cfg: cfgmod.Config):
        self.dirs: dict[str, Path] = {}
        self.manifests: dict[str, dict] = {}
        for root in conspec.index_dirs(cfg):
            if not root.is_dir():
                continue
            for d in sorted(root.iterdir()):
                ij = d / "index.json"
                if not ij.is_file() or d.name in self.dirs:
                    continue
                try:
                    self.manifests[d.name] = json.loads(ij.read_text())
                    self.dirs[d.name] = d
                except (OSError, json.JSONDecodeError):
                    continue

    def slugs_for_mpn(self, mpn: str) -> list[str]:
        if not mpn:
            return []
        out = []
        for slug, m in self.manifests.items():
            own = m.get("mpn") or ""
            if own and conspec.mpn_matches(mpn, own):
                out.append(slug)
        return sorted(out)

    def page_text(self, slug: str, page: int) -> str | None:
        d = self.dirs.get(slug)
        if not d:
            return None
        full = d / "text" / "full.txt"
        if not full.is_file():
            return None
        pages = full.read_text(errors="replace").split("\f")
        if not 1 <= page <= len(pages):
            return None
        return pages[page - 1]

    def page_png(self, slug: str, page: int) -> bytes | None:
        d = self.dirs.get(slug)
        m = self.manifests.get(slug)
        if not d or not m or not m.get("pdf"):
            return None
        root = d.parent.parent
        pdf = root / m["pdf"]
        if not pdf.is_file():
            return None
        try:
            png = dsmod._render(pdf, page, IMAGE_DPI, root / dsmod.CACHE_DIRNAME)
            return png.read_bytes()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_FINDING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "severity": {"type": "string", "enum": list(_SEVERITIES)},
        "message": {"type": "string"},
        "why": {"type": "string"},
        "pages": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"slug": {"type": "string"}, "page": {"type": "integer"}},
            "required": ["slug", "page"],
        }},
        "recommendation": {"type": "string"},
    },
    "required": ["severity", "message", "why", "pages", "recommendation"],
}

TOOLS = [
    {"name": "get_component",
     "description": "A component's value, MPN, and pin->net map from the design graph.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string"}},
                      "required": ["ref"]}},
    {"name": "get_net",
     "description": "A net's kind (power/gnd/signal), inferred voltage, and every (ref, pin) on it.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "find_connected",
     "description": "Every net a component touches and the peer pins on each — its circuit neighborhood.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string"}},
                      "required": ["ref"]}},
    {"name": "get_constraints",
     "description": "The extracted constraints (pin table, abs-max, recommended, specs) for a component's MPN, if ingested.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string"}},
                      "required": ["ref"]}},
    {"name": "read_datasheet",
     "description": ("One datasheet page, by slug and 1-based page number. Text by default; "
                     "set as_image=true for the rendered page image (tables, figures, "
                     "pinout drawings). Reads are budgeted — spend them on pages that "
                     "decide a finding. You must read a page before citing it."),
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"slug": {"type": "string"},
                                     "page": {"type": "integer"},
                                     "as_image": {"type": "boolean"}},
                      "required": ["slug", "page"]}},
    {"name": "submit_review",
     "description": "File the review's findings. Call exactly once, when the review is complete.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"findings": {"type": "array",
                                                  "items": _FINDING_SCHEMA}},
                      "required": ["findings"]}},
]


class ReviewSession:
    """Per-IC tool-execution state: graph queries, budgets, pages actually read."""

    def __init__(self, g: graphmod.DesignGraph, lookup, lib: DatasheetLibrary,
                 own_slugs: list[str], page_budget: int, neighbor_budget: int):
        self.g = g
        self.lookup = lookup
        self.lib = lib
        self.own_slugs = set(own_slugs)
        self.page_budget = page_budget
        self.neighbor_budget = neighbor_budget
        self.pages_read: set[tuple[str, int]] = set()
        self.per_slug: dict[str, int] = {}
        self.findings: list[dict] | None = None

    # -- individual tools -------------------------------------------------

    def _get_component(self, ref: str):
        c = self.g.components.get(ref)
        if not c:
            return f"no component {ref!r} in the design"
        return json.dumps({"ref": c.ref, "value": c.value, "mpn": c.mpn,
                           "pins": c.pins}, sort_keys=True)

    def _get_net(self, name: str):
        n = self.g.nets.get(name)
        if not n:
            return f"no net {name!r} in the design"
        return json.dumps({"name": n.name, "kind": n.kind, "voltage": n.voltage,
                           "pins": n.pins}, sort_keys=True)

    def _find_connected(self, ref: str):
        if ref not in self.g.components:
            return f"no component {ref!r} in the design"
        out = {}
        for net_name, peers in sorted(self.g.neighbors(ref).items()):
            n = self.g.nets.get(net_name)
            out[net_name] = {"kind": n.kind if n else "signal",
                             "voltage": n.voltage if n else None,
                             "peers": [f"{r}.{p}" for r, p in peers]}
        return json.dumps(out, sort_keys=True)

    def _get_constraints(self, ref: str):
        c = self.g.components.get(ref)
        if not c or not c.mpn:
            return f"no MPN known for {ref!r}"
        cons = self.lookup(c.mpn)
        if not cons:
            return f"no constraints ingested for {c.mpn!r}"
        return json.dumps({k: v for k, v in cons.items() if k != "_dir"},
                          sort_keys=True)

    def _read_datasheet(self, slug: str, page: int, as_image: bool = False):
        if len(self.pages_read) >= self.page_budget and (slug, page) not in self.pages_read:
            return "page budget exhausted — work with what you have read"
        if slug not in self.own_slugs and (slug, page) not in self.pages_read:
            if self.per_slug.get(slug, 0) >= self.neighbor_budget:
                return (f"per-neighbor page budget for {slug!r} exhausted — "
                        "work with what you have read")
        if as_image:
            png = self.lib.page_png(slug, page)
            if png is None:
                return f"cannot render {slug!r} page {page}"
            self._count(slug, page)
            return [{"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                "data": base64.standard_b64encode(png).decode()}},
                    {"type": "text", "text": f"[{slug} page {page}, rendered]"}]
        text = self.lib.page_text(slug, page)
        if text is None:
            return f"no such datasheet page: {slug!r} p{page}"
        self._count(slug, page)
        return f"--- {slug} page {page} ---\n{text}"

    def _count(self, slug: str, page: int) -> None:
        if (slug, page) not in self.pages_read:
            self.pages_read.add((slug, page))
            if slug not in self.own_slugs:
                self.per_slug[slug] = self.per_slug.get(slug, 0) + 1

    # -- dispatch ---------------------------------------------------------

    def execute(self, name: str, tool_input: dict):
        try:
            if name == "get_component":
                return self._get_component(tool_input["ref"])
            if name == "get_net":
                return self._get_net(tool_input["name"])
            if name == "find_connected":
                return self._find_connected(tool_input["ref"])
            if name == "get_constraints":
                return self._get_constraints(tool_input["ref"])
            if name == "read_datasheet":
                return self._read_datasheet(tool_input["slug"],
                                            int(tool_input["page"]),
                                            bool(tool_input.get("as_image")))
            if name == "submit_review":
                self.findings = tool_input.get("findings", [])
                return "review recorded"
            return f"unknown tool {name!r}"
        except Exception as e:      # a bad tool call must not kill the review
            return f"tool error: {e}"


# ---------------------------------------------------------------------------
# Normalization — downgrade-only, code-enforced
# ---------------------------------------------------------------------------

def normalize(findings: list[dict], pages_read: set[tuple[str, int]]) -> list[dict]:
    """Enforce the severity rules in code. Only ever demotes, never promotes."""
    out: list[dict] = []
    seen_msgs: set[str] = set()
    for f in findings or []:
        sev = str(f.get("severity", "")).lower()
        if sev not in _SEVERITIES:
            sev = "warning"
        why = str(f.get("why", ""))
        pages = [p for p in (f.get("pages") or [])
                 if isinstance(p, dict) and "slug" in p and "page" in p]
        cited_read = [p for p in pages if (p["slug"], int(p["page"])) in pages_read]

        if sev == "error":
            if why.lstrip().lower().startswith("unverified"):
                sev = "warning"
            elif not cited_read:
                sev = "warning"
                why = ("Unverified: no cited datasheet page was actually read "
                       "during this review. " + why)

        msg = str(f.get("message", "")).strip()
        key = re.sub(r"\s+", " ", msg.lower())
        if not msg or key in seen_msgs:
            continue                 # empty or exact-duplicate finding
        seen_msgs.add(key)
        out.append({"severity": sev, "message": msg, "why": why,
                    "pages": pages,
                    "recommendation": str(f.get("recommendation", ""))})
    return out


def dedup_across(ics: list[dict]) -> None:
    """Collapse one physical defect reported from both ends of an interface:
    identical normalized message text appearing under two ICs keeps the first."""
    seen: set[str] = set()
    for ic in ics:
        kept = []
        for f in ic.get("findings", []):
            key = re.sub(r"\s+", " ", f["message"].lower())
            if key in seen:
                continue
            seen.add(key)
            kept.append(f)
        ic["findings"] = kept


# ---------------------------------------------------------------------------
# The per-IC review loop
# ---------------------------------------------------------------------------

def _graph_summary(g: graphmod.DesignGraph) -> str:
    """Deterministic one-block design summary (stable bytes -> cacheable)."""
    lines = ["## Design summary", "", "Components:"]
    for ref in sorted(g.components):
        c = g.components[ref]
        lines.append(f"  {ref}: {c.value or '?'}"
                     + (f" [{c.mpn}]" if c.mpn else "")
                     + f" ({len(c.pins)} pins)")
    lines.append("")
    lines.append("Power nets:")
    for name in sorted(g.nets):
        n = g.nets[name]
        if n.kind != "signal":
            v = f"{n.voltage:g}V" if n.voltage is not None else "?"
            lines.append(f"  {name}: {n.kind} {v} ({len(n.pins)} pins)")
    return "\n".join(lines)


def _ic_ask(g: graphmod.DesignGraph, ref: str, own_slugs: list[str],
            all_slugs: dict[str, list[str]]) -> str:
    c = g.components[ref]
    lines = [f"Review {ref} ({c.value}, MPN {c.mpn or 'unknown'}).",
             "",
             f"Its datasheet slug(s): {', '.join(own_slugs) or 'none ingested'}."]
    neigh = sorted({r for peers in g.neighbors(ref).values() for r, _ in peers})
    with_ds = [f"{r} -> {', '.join(all_slugs[r])}"
               for r in neigh if all_slugs.get(r)]
    if with_ds:
        lines.append("Connected components with ingested datasheets: "
                     + "; ".join(with_ds) + ".")
    lines.append("")
    lines.append("Investigate with the tools, then call submit_review once.")
    return "\n".join(lines)


def review_component(client, model: str, g: graphmod.DesignGraph,
                     session: ReviewSession, graph_summary: str,
                     ask: str, usage_log=None) -> tuple[list[dict], dict]:
    """Run one IC's tool loop. Returns (raw findings, usage totals)."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": graph_summary,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": ask},
    ]}]
    totals = {"input_tokens": 0, "output_tokens": 0,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        )
        u = getattr(response, "usage", None)
        if u is not None:
            for k in totals:
                totals[k] += getattr(u, k, 0) or 0
            if usage_log:
                usage_log(totals | {"turn_input": getattr(u, "input_tokens", 0)})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            break                                   # end_turn without submitting
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for tu in tool_uses:
            content = session.execute(tu.name, tu.input)
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": content})
        messages.append({"role": "user", "content": results})
        if session.findings is not None:
            break                                   # submit_review seen

    return (session.findings if session.findings is not None else []), totals


# ---------------------------------------------------------------------------
# Whole-run orchestration
# ---------------------------------------------------------------------------

def select_ics(g: graphmod.DesignGraph, min_pins: int,
               only: str | None = None) -> list[str]:
    if only:
        if only not in g.components:
            sys.exit(f"error: no component {only!r} in the design")
        return [only]
    return [ref for ref in sorted(g.components)
            if ref.startswith("U") and len(g.components[ref].pins) >= min_pins]


def run_review(cfg: cfgmod.Config, client, model: str,
               only: str | None = None, progress=print) -> dict:
    g = graphmod.build(cfg.root_sch)
    lookup = conspec.lookup(cfg)
    lib = DatasheetLibrary(cfg)
    rc = _review_cfg(cfg)
    refs = select_ics(g, rc["min_pins"], only)
    graph_summary = _graph_summary(g)
    all_slugs = {ref: lib.slugs_for_mpn(g.components[ref].mpn)
                 for ref in g.components}

    usage_path = Path(cfg.root) / ".audit-cache" / "review-usage.jsonl"
    usage_path.parent.mkdir(exist_ok=True)

    ics: list[dict] = []
    for ref in refs:
        own = all_slugs.get(ref, [])
        session = ReviewSession(g, lookup, lib, own,
                                rc["page_budget"], rc["neighbor_budget"])
        ask = _ic_ask(g, ref, own, all_slugs)
        progress(f"reviewing {ref} ({g.components[ref].mpn or g.components[ref].value})…")

        def _log(u, _ref=ref):
            with usage_path.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "ref": _ref,
                                    "model": model, **u}) + "\n")

        try:
            raw, totals = review_component(client, model, g, session,
                                           graph_summary, ask, usage_log=_log)
            findings = normalize(raw, session.pages_read)
            status = "ok" if session.findings is not None else "no_submission"
            ics.append({"ref": ref, "mpn": g.components[ref].mpn,
                        "status": status, "findings": findings,
                        "pages_read": sorted([list(p) for p in session.pages_read]),
                        "usage": totals})
        except Exception as e:                      # per-IC isolation
            ics.append({"ref": ref, "mpn": g.components[ref].mpn,
                        "status": "failed", "error": str(e), "findings": []})
            progress(f"  {ref} FAILED: {e}")

    return _finish(cfg, ics, model)


def _finish(cfg: cfgmod.Config, ics: list[dict], model: str | None) -> dict:
    """Dedup + persist the review artifact — shared by both backends."""
    dedup_across(ics)
    # newest mtime across ALL sheets — must match BoardState.sch_mtime(), else
    # the cockpit would report a fresh review as stale whenever a child sheet
    # is newer than the root
    root_sch = Path(cfg.root_sch)
    sch_mtime = max((p.stat().st_mtime for p in root_sch.parent.glob("*.kicad_sch")),
                    default=root_sch.stat().st_mtime)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sch_mtime": sch_mtime,
        "model": model or "claude-code default",
        "ics": ics,
    }
    out = Path(cfg.root) / ".cockpit" / "review.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    return report


# ---------------------------------------------------------------------------
# claude-code backend — drive `claude -p` with the same tools over MCP
# ---------------------------------------------------------------------------

_TOOL_DISCIPLINE = """\

Use ONLY the mcp__kbreview__* tools — the design graph, extracted constraints, \
and datasheet pages are all behind them, and citing a page requires having read \
it through mcp__kbreview__read_datasheet. Do not use any other tool. Finish by \
calling mcp__kbreview__submit_review exactly once."""


def claude_bin() -> str | None:
    """The claude CLI, tolerating minimal daemon PATHs (systemd services don't
    have ~/.local/bin, where the CLI installs)."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home() / ".local" / "bin" / "claude"
    return str(home) if home.is_file() else None


def _claude_cmd(prompt: str, mcp_cfg: Path, model: str | None) -> list[str]:
    allowed = ",".join(f"mcp__kbreview__{t['name']}" for t in TOOLS)
    cmd = [claude_bin() or "claude", "-p", prompt,
           "--append-system-prompt", SYSTEM_PROMPT,
           "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
           "--allowedTools", allowed,
           "--disallowedTools", _CLAUDE_DISALLOWED,
           "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    return cmd


def review_component_claude_code(cfg: cfgmod.Config, ref: str, prompt: str,
                                 model: str | None) -> tuple[dict | None, dict]:
    """One IC via `claude -p` + the kbreview MCP server.

    Returns (session state {findings, pages_read} | None, run meta)."""
    workdir = Path(tempfile.mkdtemp(prefix=f"kbreview-{ref}-"))
    try:
        out = workdir / "session.json"
        mcp_cfg = workdir / "mcp.json"
        mcp_cfg.write_text(json.dumps({"mcpServers": {"kbreview": {
            "type": "stdio", "command": sys.executable,
            "args": ["-m", "kicad_bench.review_mcp",
                     "--config", str(cfg.path), "--ref", ref,
                     "--out", str(out)],
        }}}))
        proc = subprocess.run(_claude_cmd(prompt, mcp_cfg, model),
                              capture_output=True, text=True,
                              cwd=cfg.root, timeout=CLAUDE_TIMEOUT)
        try:
            meta = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            meta = {"is_error": True,
                    "result": (proc.stderr or proc.stdout or "")[-2000:]}
        state = None
        if out.is_file():
            try:
                state = json.loads(out.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        if state is None and proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: "
                               f"{(proc.stderr or '')[-500:]}")
        return state, meta
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_review_claude_code(cfg: cfgmod.Config, model: str | None = None,
                           only: str | None = None, progress=print) -> dict:
    """Whole-run orchestration on the claude-code backend — same selection,
    normalization, persistence, and usage logging as the API path."""
    g = graphmod.build(cfg.root_sch)
    lib = DatasheetLibrary(cfg)
    rc = _review_cfg(cfg)
    refs = select_ics(g, rc["min_pins"], only)
    graph_summary = _graph_summary(g)
    all_slugs = {ref: lib.slugs_for_mpn(g.components[ref].mpn)
                 for ref in g.components}

    usage_path = Path(cfg.root) / ".audit-cache" / "review-usage.jsonl"
    usage_path.parent.mkdir(exist_ok=True)

    ics: list[dict] = []
    for ref in refs:
        own = all_slugs.get(ref, [])
        prompt = (graph_summary + "\n\n" + _ic_ask(g, ref, own, all_slugs)
                  + _TOOL_DISCIPLINE)
        progress(f"reviewing {ref} ({g.components[ref].mpn or g.components[ref].value}) "
                 f"via claude-code…")
        try:
            state, meta = review_component_claude_code(cfg, ref, prompt, model)
            raw = (state or {}).get("findings")
            pages_read = {tuple(p) for p in (state or {}).get("pages_read", [])}
            findings = normalize(raw or [], pages_read)
            status = "ok" if raw is not None else "no_submission"
            ics.append({"ref": ref, "mpn": g.components[ref].mpn,
                        "status": status, "findings": findings,
                        "pages_read": sorted([list(p) for p in pages_read]),
                        "usage": (meta or {}).get("usage") or {}})
            with usage_path.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "ref": ref,
                                    "backend": "claude-code",
                                    "model": model or "default",
                                    "cost_usd": (meta or {}).get("total_cost_usd"),
                                    "duration_ms": (meta or {}).get("duration_ms")})
                        + "\n")
        except Exception as e:                  # per-IC isolation
            ics.append({"ref": ref, "mpn": g.components[ref].mpn,
                        "status": "failed", "error": str(e), "findings": []})
            progress(f"  {ref} FAILED: {e}")

    return _finish(cfg, ics, model)


def run_review_auto(cfg: cfgmod.Config, model: str | None = None,
                    only: str | None = None, backend: str | None = None,
                    progress=print) -> dict:
    """Backend dispatch: claude-code (default, subscription auth) or api."""
    backend = backend or review_backend(cfg)
    # always resolve to an explicit model — never inherit the user's claude CLI
    # session default (which may be a pricier tier)
    model = model or review_model(cfg) or DEFAULT_MODEL
    if backend == "api":
        return run_review(cfg, _client(), model, only=only, progress=progress)
    if backend != "claude-code":
        sys.exit(f"error: unknown review backend {backend!r} "
                 "(expected 'claude-code' or 'api')")
    if not claude_bin():
        sys.exit("error: `claude` CLI not found — install Claude Code or use "
                 "--backend api with an ANTHROPIC_API_KEY")
    return run_review_claude_code(cfg, model, only=only, progress=progress)


def to_result(report: dict) -> Result:
    res = Result("Datasheet review")
    n_err = n_warn = 0
    for ic in report["ics"]:
        if ic["status"] == "failed":
            res.info(f"{ic['ref']} — review failed: {ic.get('error', '?')}",
                     ic["ref"])
            continue
        for f in ic["findings"]:
            cite = ", ".join(f"{p['slug']} p{p['page']}" for p in f["pages"])
            detail = f["why"] + (f"  [{cite}]" if cite else "")
            if f["severity"] == "error":
                n_err += 1
                res.error(f["message"], ic["ref"], detail)
            elif f["severity"] == "warning":
                n_warn += 1
                res.warn(f["message"], ic["ref"], detail)
            else:
                res.info(f["message"], ic["ref"], detail)
    res.summary = (f"{len(report['ics'])} IC(s) reviewed with {report['model']}; "
                   f"{n_err} error(s), {n_warn} warning(s)")
    return res


def _client():
    try:
        import anthropic
    except ImportError:
        sys.exit("error: the `anthropic` package is required for kb review — "
                 "pip install 'kicad-bench[llm]'")
    return anthropic.Anthropic()


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    if not cfg.root_sch or not Path(cfg.root_sch).exists():
        sys.exit(f"error: schematic not found: {cfg.root_sch}")
    report = run_review_auto(cfg, model=args.model, only=args.ref,
                             backend=args.backend)
    return render_and_exit(to_result(report))


def add_parser(sub):
    p = sub.add_parser("review",
                       help="datasheet-grounded per-IC review (claude-code CLI by default; "
                            "--backend api uses ANTHROPIC_API_KEY)")
    p.add_argument("--ref", help="review a single IC (default: all with >= [review].min_pins pins)")
    p.add_argument("--backend", choices=["claude-code", "api"],
                   help=f"model backend (default: [llm].backend or {DEFAULT_BACKEND})")
    p.add_argument("--model", help="override model (claude-code: CLI session default; "
                                   f"api: {DEFAULT_MODEL})")
    p.add_argument("--board", help="which board (multi-board configs)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
