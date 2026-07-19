"""review_mcp — stdio MCP server exposing one IC's ReviewSession tools.

Spawned per-IC by the `claude-code` review backend: `claude -p` connects over
stdio and gets exactly the same tool surface (and page budgets) the API backend
gives the model — graph queries, budgeted datasheet reads, submit_review. After
every tool call the session state (findings + pages actually read) is flushed to
`--out`, so the parent `kb review` process can normalize findings after the
Claude Code run exits, identically to the API path.

Minimal hand-rolled MCP: newline-delimited JSON-RPC 2.0 on stdio implementing
initialize / tools/list / tools/call / ping. Kept stdlib-only on purpose (kb's
core has no MCP dependency, and the protocol subset needed here is tiny).

Not a user-facing command — invoked as `python -m kicad_bench.review_mcp`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import config as cfgmod, conspec, graph as graphmod
from . import review as reviewmod

PROTOCOL_VERSION = "2024-11-05"


def _mcp_tools() -> list[dict]:
    """review.TOOLS in MCP shape (inputSchema key, camelCase)."""
    return [{"name": t["name"], "description": t["description"],
             "inputSchema": t["input_schema"]} for t in reviewmod.TOOLS]


def _to_mcp_content(result) -> list[dict]:
    """ReviewSession.execute output (str, or Anthropic-style block list) -> MCP
    content blocks."""
    if isinstance(result, str):
        return [{"type": "text", "text": result}]
    out = []
    for block in result:
        if block.get("type") == "image":
            src = block["source"]
            out.append({"type": "image", "data": src["data"],
                        "mimeType": src["media_type"]})
        else:
            out.append({"type": "text", "text": block.get("text", "")})
    return out


class Server:
    def __init__(self, session: reviewmod.ReviewSession, out_path: Path):
        self.session = session
        self.out_path = out_path

    def _flush(self) -> None:
        """Persist session state for the parent process (atomic-ish, best effort)."""
        try:
            tmp = self.out_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "findings": self.session.findings,
                "pages_read": sorted([list(p) for p in self.session.pages_read]),
            }))
            tmp.replace(self.out_path)
        except OSError:
            pass

    def handle(self, req: dict) -> dict | None:
        """One JSON-RPC request -> response dict (None for notifications)."""
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "kbreview", "version": "1"}}
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _mcp_tools()}
        elif method == "tools/call":
            params = req.get("params") or {}
            content = self.session.execute(params.get("name", ""),
                                           params.get("arguments") or {})
            self._flush()
            result = {"content": _to_mcp_content(content)}
        else:
            if rid is None:
                return None            # unknown notification — ignore
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown method {method!r}"}}
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(req)
            if resp is not None:
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()
        self._flush()                  # client hung up — final state dump


def build_session(cfg: cfgmod.Config, ref: str) -> reviewmod.ReviewSession:
    g = graphmod.build(cfg.root_sch)
    graphmod.solve_rail_voltages(g, conspec.lookup(cfg))
    lib = reviewmod.DatasheetLibrary(cfg)
    rc = reviewmod._review_cfg(cfg)
    comp = g.components.get(ref)
    own = lib.slugs_for_mpn(conspec.part_key(comp)) if comp else []
    return reviewmod.ReviewSession(g, conspec.lookup(cfg), lib, own,
                                   rc["page_budget"], rc["neighbor_budget"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--board", default=None)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    cfg = cfgmod.load_or_exit(args.config, args.board)
    server = Server(build_session(cfg, args.ref), Path(args.out))
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
