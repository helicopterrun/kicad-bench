"""Tests for kb review — fake client replaying canned tool-use transcripts.

No network, no anthropic dependency: the engine only needs an object with
`.messages.create(**kwargs)` returning response-shaped objects.
"""
import json
from types import SimpleNamespace

import pytest

from kicad_bench import review
from kicad_bench.core import graph as graphmod


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _resp(blocks, stop="tool_use"):
    usage = SimpleNamespace(input_tokens=100, output_tokens=50,
                            cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage)


class FakeClient:
    """Replays a scripted list of responses; records every request."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


class FakeLib:
    """Datasheet library with two 3-page docs of synthetic text."""
    def __init__(self):
        self.docs = {"mcu-ds": ["p1 text", "abs max 3.6V", "pinout"],
                     "bridge-ds": ["p1", "vout high 4.5V", "p3"]}

    def slugs_for_mpn(self, mpn):
        return {"MCU123X": ["mcu-ds"], "BRG55Y": ["bridge-ds"]}.get(mpn, [])

    def page_text(self, slug, page):
        pages = self.docs.get(slug)
        if pages and 1 <= page <= len(pages):
            return pages[page - 1]
        return None

    def page_png(self, slug, page):
        return b"\x89PNG fake" if self.page_text(slug, page) else None


def _graph():
    counts = {"+5V": ["U2.7"], "+3V3": ["U1.5"], "GND": ["U1.2", "U2.3"],
              "UART_TX": ["U1.7", "U2.10"]}
    pin_net = {}
    for net, refpins in counts.items():
        for rp in refpins:
            r, _, p = rp.partition(".")
            pin_net[(r, p)] = net
    comps = [{"reference": "U1", "value": "MCU", "mpn": "MCU123X"},
             {"reference": "U2", "value": "Bridge", "mpn": "BRG55Y"}]
    return graphmod.build_from(counts, pin_net, comps)


def _session(own=("mcu-ds",), budget=24, neighbor=8):
    return review.ReviewSession(_graph(), lambda mpn: None, FakeLib(),
                                list(own), budget, neighbor)


# ---------------------------------------------------------------------------
# normalize() — downgrade-only discipline
# ---------------------------------------------------------------------------

def test_normalize_error_without_read_pages_demoted():
    out = review.normalize(
        [{"severity": "error", "message": "abs max exceeded",
          "why": "5V into a 3.6V pin", "pages": [{"slug": "mcu-ds", "page": 2}],
          "recommendation": "add level shifter"}],
        pages_read=set())                     # cited page was never read
    assert out[0]["severity"] == "warning"
    assert out[0]["why"].startswith("Unverified:")


def test_normalize_error_with_read_pages_stays():
    out = review.normalize(
        [{"severity": "error", "message": "abs max exceeded",
          "why": "5V vs 3.6V limit", "pages": [{"slug": "mcu-ds", "page": 2}],
          "recommendation": "fix"}],
        pages_read={("mcu-ds", 2)})
    assert out[0]["severity"] == "error"


def test_normalize_unverified_error_demoted():
    out = review.normalize(
        [{"severity": "error", "message": "m",
          "why": "Unverified: rail voltage unknown",
          "pages": [{"slug": "mcu-ds", "page": 2}], "recommendation": ""}],
        pages_read={("mcu-ds", 2)})
    assert out[0]["severity"] == "warning"


def test_normalize_never_upgrades_and_fixes_bad_severity():
    out = review.normalize(
        [{"severity": "info", "message": "a", "why": "", "pages": [], "recommendation": ""},
         {"severity": "CRITICAL", "message": "b", "why": "", "pages": [], "recommendation": ""}],
        pages_read=set())
    assert out[0]["severity"] == "info"       # info never touched
    assert out[1]["severity"] == "warning"    # unknown severity -> warning


def test_normalize_drops_duplicates_and_empties():
    out = review.normalize(
        [{"severity": "info", "message": "Same thing", "why": "", "pages": [], "recommendation": ""},
         {"severity": "info", "message": "same  THING", "why": "", "pages": [], "recommendation": ""},
         {"severity": "info", "message": "", "why": "", "pages": [], "recommendation": ""}],
        pages_read=set())
    assert len(out) == 1


def test_dedup_across_ics():
    ics = [{"ref": "U1", "findings": [{"message": "TX/RX voltage mismatch"}]},
           {"ref": "U2", "findings": [{"message": "tx/rx  voltage mismatch"},
                                      {"message": "unique to U2"}]}]
    review.dedup_across(ics)
    assert len(ics[0]["findings"]) == 1
    assert [f["message"] for f in ics[1]["findings"]] == ["unique to U2"]


# ---------------------------------------------------------------------------
# ReviewSession — tools & budgets
# ---------------------------------------------------------------------------

def test_graph_tools():
    s = _session()
    assert json.loads(s.execute("get_component", {"ref": "U1"}))["mpn"] == "MCU123X"
    assert json.loads(s.execute("get_net", {"name": "+5V"}))["voltage"] == 5.0
    conn = json.loads(s.execute("find_connected", {"ref": "U1"}))
    assert "U2.10" in conn["UART_TX"]["peers"]
    assert "no component" in s.execute("get_component", {"ref": "ZZ"})


def test_read_datasheet_text_and_tracking():
    s = _session()
    out = s.execute("read_datasheet", {"slug": "mcu-ds", "page": 2})
    assert "abs max 3.6V" in out
    assert ("mcu-ds", 2) in s.pages_read


def test_read_datasheet_image_blocks():
    s = _session()
    out = s.execute("read_datasheet", {"slug": "mcu-ds", "page": 3, "as_image": True})
    assert isinstance(out, list) and out[0]["type"] == "image"


def test_global_page_budget():
    s = _session(budget=2)
    s.execute("read_datasheet", {"slug": "mcu-ds", "page": 1})
    s.execute("read_datasheet", {"slug": "mcu-ds", "page": 2})
    out = s.execute("read_datasheet", {"slug": "mcu-ds", "page": 3})
    assert "budget exhausted" in out
    # re-reading an already-read page is still allowed
    assert "p1 text" in s.execute("read_datasheet", {"slug": "mcu-ds", "page": 1})


def test_neighbor_budget():
    s = _session(own=("mcu-ds",), budget=24, neighbor=1)
    s.execute("read_datasheet", {"slug": "bridge-ds", "page": 1})
    out = s.execute("read_datasheet", {"slug": "bridge-ds", "page": 2})
    assert "per-neighbor" in out
    # own datasheet unaffected by the neighbor budget
    assert "abs max" in s.execute("read_datasheet", {"slug": "mcu-ds", "page": 2})


def test_bad_tool_call_is_soft_error():
    s = _session()
    assert "tool error" in s.execute("read_datasheet", {"slug": "mcu-ds"})  # no page


# ---------------------------------------------------------------------------
# review_component — the loop
# ---------------------------------------------------------------------------

FINDING = {"severity": "error", "message": "5V bridge output into 3.6V-max pin",
           "why": "condition/limit/harm per p2", "pages": [{"slug": "mcu-ds", "page": 2}],
           "recommendation": "level-shift"}


def test_loop_runs_tools_then_submit():
    client = FakeClient([
        _resp([_text("looking"), _tool_use("t1", "get_component", {"ref": "U1"}),
               _tool_use("t2", "read_datasheet", {"slug": "mcu-ds", "page": 2})]),
        _resp([_tool_use("t3", "submit_review", {"findings": [FINDING]})]),
    ])
    s = _session()
    raw, totals = review.review_component(client, "m", _graph(), s,
                                          "summary", "ask")
    assert raw == [FINDING]
    assert totals["input_tokens"] == 200          # two calls
    # parallel tool results returned in ONE user message
    second_request = client.requests[1]
    results_msg = second_request["messages"][-2]  # [-1] is the submit turn's results? check below
    # structure: [user(ask), assistant(turn1), user(2 tool_results), assistant(turn2), user(1 result)]
    assert [m["role"] for m in client.requests[1]["messages"]][:3] == ["user", "assistant", "user"]
    tr = client.requests[1]["messages"][2]["content"]
    assert len(tr) == 2 and all(x["type"] == "tool_result" for x in tr)
    # findings recorded and cited page counted as read
    assert ("mcu-ds", 2) in s.pages_read
    # normalized finding keeps error (cited page was read)
    assert review.normalize(raw, s.pages_read)[0]["severity"] == "error"


def test_loop_end_turn_without_submit():
    client = FakeClient([_resp([_text("nothing to report")], stop="end_turn")])
    s = _session()
    raw, _ = review.review_component(client, "m", _graph(), s, "summary", "ask")
    assert raw == [] and s.findings is None


def test_prompt_caching_and_stable_prefix():
    client = FakeClient([_resp([_text("done")], stop="end_turn")])
    s = _session()
    review.review_component(client, "m", _graph(), s, "GRAPH", "ask")
    req = client.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["thinking"] == {"type": "adaptive"}
    assert req["tools"] is review.TOOLS


# ---------------------------------------------------------------------------
# run_review — orchestration, isolation, persistence
# ---------------------------------------------------------------------------

def _fake_cfg(tmp_path):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text("(kicad_sch)")
    return SimpleNamespace(root=tmp_path, root_sch=sch, raw={})


def test_run_review_persists_and_isolates(tmp_path, monkeypatch):
    monkeypatch.setattr(graphmod, "build", lambda sch: _graph())
    monkeypatch.setattr(review, "DatasheetLibrary", lambda cfg: FakeLib())
    monkeypatch.setattr(review.conspec, "lookup", lambda cfg: (lambda mpn: None))

    calls = {"n": 0}

    class FlakyClient(FakeClient):
        def create(self, **kw):
            # first IC (U1) submits; second IC (U2) blows up on its first call
            calls["n"] += 1
            if calls["n"] == 1:
                return _resp([_tool_use("t", "submit_review",
                                        {"findings": [dict(FINDING, pages=[])]})])
            raise RuntimeError("api down")

    cfg = _fake_cfg(tmp_path)
    # min_pins default is 8; our fake ICs have <8 pins -> use --ref-like config
    cfg.raw = {"review": {"min_pins": 1}}
    report = review.run_review(cfg, FlakyClient([]), "test-model",
                               progress=lambda *_: None)

    assert [ic["status"] for ic in report["ics"]] == ["ok", "failed"]
    # persisted artifact
    saved = json.loads((tmp_path / ".cockpit" / "review.json").read_text())
    assert saved["model"] == "test-model" and len(saved["ics"]) == 2
    # U1's error had no read pages -> demoted to warning by normalize
    assert saved["ics"][0]["findings"][0]["severity"] == "warning"
    # usage log written
    assert (tmp_path / ".audit-cache" / "review-usage.jsonl").exists()


def test_to_result_severities(tmp_path):
    report = {"model": "m", "ics": [
        {"ref": "U1", "status": "ok", "findings": [
            {"severity": "error", "message": "e", "why": "w",
             "pages": [{"slug": "s", "page": 1}], "recommendation": "r"},
            {"severity": "warning", "message": "wn", "why": "", "pages": [],
             "recommendation": ""}]},
        {"ref": "U2", "status": "failed", "error": "api down", "findings": []},
    ]}
    res = review.to_result(report)
    assert res.n_errors == 1
    assert any(f.severity == "info" and "failed" in f.message for f in res.findings)


def test_finish_merges_subset_into_existing_artifact(tmp_path):
    """--ref runs must not clobber the other ICs' findings in review.json."""
    cfg = _fake_cfg(tmp_path)
    prev = {"model": "m", "sch_mtime": 0, "ics": [
        {"ref": "U1", "status": "ok", "findings": [{"severity": "info",
         "message": "keep me", "why": "", "pages": [], "recommendation": ""}]},
        {"ref": "U5", "status": "failed", "error": "old", "findings": []},
    ]}
    out = tmp_path / ".cockpit" / "review.json"
    out.parent.mkdir()
    out.write_text(json.dumps(prev))

    report = review._finish(cfg, [{"ref": "U5", "status": "ok", "findings": []}], "m2")
    refs = [ic["ref"] for ic in report["ics"]]
    assert refs == ["U1", "U5"]
    assert report["ics"][1]["status"] == "ok"          # U5 replaced
    assert report["ics"][0]["findings"][0]["message"] == "keep me"
