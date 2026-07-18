"""Tests for the kbreview stdio MCP server + the claude-code backend plumbing."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_bench import review, review_mcp
from tests.test_review import FakeLib, _graph, FINDING


def _server(tmp_path, own=("mcu-ds",)):
    session = review.ReviewSession(_graph(), lambda mpn: None, FakeLib(),
                                   list(own), 24, 8)
    return review_mcp.Server(session, tmp_path / "out.json")


def _rpc(server, method, params=None, id=1):
    return server.handle({"jsonrpc": "2.0", "id": id, "method": method,
                          "params": params or {}})


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

def test_initialize_and_tools_list(tmp_path):
    s = _server(tmp_path)
    init = _rpc(s, "initialize")
    assert init["result"]["serverInfo"]["name"] == "kbreview"
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = _rpc(s, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {t["name"] for t in review.TOOLS}
    assert all("inputSchema" in t for t in tools)     # MCP casing, not input_schema


def test_tools_call_text_and_image(tmp_path):
    s = _server(tmp_path)
    r = _rpc(s, "tools/call", {"name": "get_component", "arguments": {"ref": "U1"}})
    block = r["result"]["content"][0]
    assert block["type"] == "text" and json.loads(block["text"])["mpn"] == "MCU123X"

    r = _rpc(s, "tools/call", {"name": "read_datasheet",
                               "arguments": {"slug": "mcu-ds", "page": 3,
                                             "as_image": True}})
    kinds = [b["type"] for b in r["result"]["content"]]
    assert "image" in kinds
    img = next(b for b in r["result"]["content"] if b["type"] == "image")
    assert img["mimeType"] == "image/png" and img["data"]


def test_unknown_method_errors(tmp_path):
    s = _server(tmp_path)
    r = _rpc(s, "resources/list")
    assert "error" in r


def test_submit_flushes_state_file(tmp_path):
    s = _server(tmp_path)
    _rpc(s, "tools/call", {"name": "read_datasheet",
                           "arguments": {"slug": "mcu-ds", "page": 2}})
    _rpc(s, "tools/call", {"name": "submit_review",
                           "arguments": {"findings": [FINDING]}})
    state = json.loads((tmp_path / "out.json").read_text())
    assert state["findings"] == [FINDING]
    assert ["mcu-ds", 2] in state["pages_read"]


def test_serve_loop_newline_json(tmp_path):
    s = _server(tmp_path)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n")
    stdout = io.StringIO()
    s.serve(stdin=stdin, stdout=stdout)
    out = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert [o["id"] for o in out] == [1, 2]           # notification unanswered
    assert (tmp_path / "out.json").exists()            # final flush on EOF


# ---------------------------------------------------------------------------
# claude-code backend orchestration (subprocess faked)
# ---------------------------------------------------------------------------

def _fake_cfg(tmp_path):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)")
    return SimpleNamespace(root=tmp_path, root_sch=sch, path=tmp_path / "kb.toml",
                           raw={"review": {"min_pins": 1}})


def test_claude_cmd_shape():
    cmd = review._claude_cmd("PROMPT", Path("/x/mcp.json"), "claude-opus-4-8")
    assert cmd[:3] == ["claude", "-p", "PROMPT"]
    assert "--strict-mcp-config" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__kbreview__submit_review" in allowed
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "Bash" in disallowed and "Read" in disallowed
    assert cmd[-2:] == ["--model", "claude-opus-4-8"]
    # no model -> no --model flag
    assert "--model" not in review._claude_cmd("p", Path("/x"), None)


def test_run_review_claude_code_ok_and_isolated(tmp_path, monkeypatch):
    from kicad_bench.core import graph as graphmod
    monkeypatch.setattr(graphmod, "build", lambda sch: _graph())
    monkeypatch.setattr(review, "DatasheetLibrary", lambda cfg: FakeLib())

    calls = []

    def fake_run_ic(cfg, ref, prompt, model):
        calls.append((ref, model))
        if ref == "U1":
            return ({"findings": [FINDING], "pages_read": [["mcu-ds", 2]]},
                    {"total_cost_usd": 0.12, "duration_ms": 5000,
                     "usage": {"output_tokens": 10}})
        raise RuntimeError("claude blew up")

    monkeypatch.setattr(review, "review_component_claude_code", fake_run_ic)
    cfg = _fake_cfg(tmp_path)
    report = review.run_review_claude_code(cfg, model=None,
                                           progress=lambda *a: None)

    assert [ic["status"] for ic in report["ics"]] == ["ok", "failed"]
    # cited page was read via MCP -> error survives normalize
    assert report["ics"][0]["findings"][0]["severity"] == "error"
    # persisted + usage log with cost
    assert (tmp_path / ".cockpit" / "review.json").exists()
    log = (tmp_path / ".audit-cache" / "review-usage.jsonl").read_text()
    assert '"backend": "claude-code"' in log and '"cost_usd": 0.12' in log
    # prompt discipline: model=None passthrough
    assert calls[0] == ("U1", None)


def test_run_review_auto_dispatch(tmp_path, monkeypatch):
    cfg = _fake_cfg(tmp_path)
    seen = {}
    monkeypatch.setattr(review, "run_review_claude_code",
                        lambda cfg, model=None, only=None, progress=print:
                        seen.setdefault("backend", "claude-code") or {"ics": []})
    monkeypatch.setattr(review.shutil, "which", lambda x: "/usr/bin/claude")
    review.run_review_auto(cfg, backend="claude-code", progress=lambda *a: None)
    assert seen["backend"] == "claude-code"

    with pytest.raises(SystemExit):
        review.run_review_auto(cfg, backend="bogus")


def test_run_review_auto_default_backend(tmp_path):
    cfg = _fake_cfg(tmp_path)
    assert review.review_backend(cfg) == "claude-code"
    cfg.raw = {"llm": {"backend": "api"}}
    assert review.review_backend(cfg) == "api"
