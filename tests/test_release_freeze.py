"""release-freeze tests — version/backend logic, board gating, and full run()
orchestration in a real git repo. kicad-cli is never needed: readiness is stubbed
and the fab export is a fake backend."""
import argparse
import subprocess
from pathlib import Path

import pytest

from kicad_bench import fab, release_freeze, scaffold
from kicad_bench.core import config as cfgmod
from kicad_bench.core.report import Result
from kicad_bench.layout import release_prep


# ── helpers ──────────────────────────────────────────────────────────────────
def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def make_repo(tmp_path, boards):
    """Scaffold a product and turn it into a committed git repo. Returns (root, cfg)."""
    root = scaffold.scaffold_product(tmp_path, "prod", boards)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.st")
    _git(root, "config", "user.name", "tester")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root, cfgmod.load(root / "kicad-bench.toml")


class FakeBackend:
    name = "fake"
    requires = "nothing"

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def export(self, cfg, out_dir, artifacts=fab.FULL):
        self.calls.append((cfg.active_board, Path(out_dir), artifacts))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "gerbers.zip").write_text("stub")
        res = Result("fab")
        res.ok("gerbers")
        return res


def _args(version, **kw):
    ns = argparse.Namespace(version=version, backend="kicad-cli", dry_run=False,
                            allow_dirty=False, no_git=False, config=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# ── pure logic ───────────────────────────────────────────────────────────────
def test_valid_version():
    for v in ("v1.0", "v0.1-eval", "v2.3.1", "v10.20.30-rc.1"):
        assert release_freeze.valid_version(v)
    for v in ("1.0", "v1", "vX.Y", "release-1", "v1.0 "):
        assert not release_freeze.valid_version(v)


def test_get_backend():
    assert isinstance(fab.get_backend(), fab.KicadCliBackend)
    assert isinstance(fab.get_backend("kicad-cli"), fab.KicadCliBackend)
    assert isinstance(fab.get_backend("kibot"), fab.KibotBackend)
    with pytest.raises(ValueError):
        fab.get_backend("nope")


def test_kibot_backend_unavailable():
    kb = fab.get_backend("kibot")
    assert kb.available() is False
    with pytest.raises(NotImplementedError):
        kb.export(None, Path("/tmp/x"))


def test_kicadcli_backend_job_set(tmp_path, monkeypatch):
    # Simulate kicad-cli: record commands, all succeed. Assert the artifact set drives
    # which jobs run (BASIC = 3 pcb jobs; FULL adds STEP, PCB PDF, sch PDF).
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(release_freeze.fab.subprocess, "run", fake_run)

    cfg = type("C", (), {"pcb": tmp_path / "b.kicad_pcb",
                         "root_sch": tmp_path / "b.kicad_sch"})()
    res_basic = fab.KicadCliBackend().export(cfg, tmp_path / "out", artifacts=fab.BASIC)
    assert res_basic.passed and len(seen) == 3          # gerbers, drill, pos
    seen.clear()
    res_full = fab.KicadCliBackend().export(cfg, tmp_path / "out2", artifacts=fab.FULL)
    assert res_full.passed and len(seen) == 6           # + step, pcb pdf, sch pdf


def test_kicadcli_backend_reports_failure(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        rc = 1 if "gerbers" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, "", "boom")

    monkeypatch.setattr(release_freeze.fab.subprocess, "run", fake_run)
    cfg = type("C", (), {"pcb": tmp_path / "b.kicad_pcb", "root_sch": None})()
    res = fab.KicadCliBackend().export(cfg, tmp_path / "out", artifacts=fab.BASIC)
    assert not res.passed
    assert any(f.severity == "error" and "gerbers" in f.message for f in res.findings)


# ── board gating ─────────────────────────────────────────────────────────────
def test_gate_boards_visits_all_and_aggregates(tmp_path, monkeypatch):
    _root, cfg = make_repo(tmp_path, ["a", "b", "c"])
    visited = []

    def stub_readiness(c):
        visited.append(c.active_board)
        ready = c.active_board != "b"          # board 'b' not releasable
        res = Result(f"readiness {c.active_board}")
        (res.ok if ready else res.error)("x")
        return ready, res, True

    monkeypatch.setattr(release_prep, "readiness", stub_readiness)
    all_ready, not_ready = release_freeze.gate_boards(cfg)
    assert visited == ["a", "b", "c"]          # every board gated
    assert all_ready is False and not_ready == ["b"]


# ── full run() orchestration ─────────────────────────────────────────────────
def _ready(c):
    r = Result("ok")
    r.ok("clean")
    return True, r, True


def test_run_freezes_all_boards_commits_and_tags(tmp_path, monkeypatch):
    root, cfg = make_repo(tmp_path, ["main-board", "sensor-board"])
    monkeypatch.setattr(release_prep, "readiness", _ready)
    backend = FakeBackend()
    monkeypatch.setattr(fab, "get_backend", lambda name: backend)
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)

    rc = release_freeze.run(_args("v1.0"))
    assert rc == 0
    # frozen fab package written per board
    for b in ("main-board", "sensor-board"):
        assert (root / "releases/v1.0" / b / "gerbers.zip").is_file()
    assert {c[0] for c in backend.calls} == {"main-board", "sensor-board"}
    # release committed + annotated tag created
    assert release_freeze._tag_exists(root, "v1.0")
    assert "Freeze release v1.0" in _git(root, "log", "-1", "--pretty=%s").stdout


def test_run_no_git_exports_despite_existing_tag(tmp_path, monkeypatch):
    # CI case: the tag already exists (it triggered the run); --no-git skips the
    # tag/clean preconditions and the commit/tag, but still gates + exports.
    root, cfg = make_repo(tmp_path, ["b1"])
    _git(root, "tag", "v1.0")
    (root / "README.md").write_text("dirty too")        # also dirty — must be ignored
    monkeypatch.setattr(release_prep, "readiness", _ready)
    backend = FakeBackend()
    monkeypatch.setattr(fab, "get_backend", lambda name: backend)
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)

    rc = release_freeze.run(_args("v1.0", no_git=True))
    assert rc == 0
    assert (root / "releases/v1.0/b1/gerbers.zip").is_file()
    # no NEW commit was made (still the single init commit)
    assert _git(root, "log", "-1", "--pretty=%s").stdout.strip() == "init"


def test_run_dry_run_writes_nothing(tmp_path, monkeypatch):
    root, cfg = make_repo(tmp_path, ["b1"])
    monkeypatch.setattr(release_prep, "readiness", _ready)
    monkeypatch.setattr(fab, "get_backend", lambda name: FakeBackend())
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)

    rc = release_freeze.run(_args("v1.0", dry_run=True))
    assert rc == 0
    assert not (root / "releases/v1.0").exists()
    assert not release_freeze._tag_exists(root, "v1.0")


def test_run_refuses_not_releasable(tmp_path, monkeypatch):
    _root, cfg = make_repo(tmp_path, ["b1"])
    monkeypatch.setattr(release_prep, "readiness",
                        lambda c: (False, _fail_result(), True))
    monkeypatch.setattr(fab, "get_backend", lambda name: FakeBackend())
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)
    assert release_freeze.run(_args("v1.0")) == 1


def _fail_result():
    r = Result("nope")
    r.error("ERC error")
    return r


def test_run_bad_version(tmp_path, monkeypatch):
    _root, cfg = make_repo(tmp_path, ["b1"])
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)
    with pytest.raises(SystemExit):
        release_freeze.run(_args("1.0"))


def test_run_refuses_existing_tag(tmp_path, monkeypatch):
    root, cfg = make_repo(tmp_path, ["b1"])
    _git(root, "tag", "v1.0")
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)
    with pytest.raises(SystemExit):
        release_freeze.run(_args("v1.0"))


def test_run_refuses_dirty_tree(tmp_path, monkeypatch):
    root, cfg = make_repo(tmp_path, ["b1"])
    (root / "README.md").write_text("dirty change")      # modify a tracked file
    monkeypatch.setattr(cfgmod, "load_or_exit", lambda *a, **k: cfg)
    with pytest.raises(SystemExit):
        release_freeze.run(_args("v1.0"))
