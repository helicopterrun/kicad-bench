"""Pure-logic tests for the sch-live IPC bridge (no running server needed)."""
import argparse

from kicad_bench import sch_live
from kicad_bench.main import build_parser


def _args(**kw):
    ns = argparse.Namespace(socket=None, json=False, timeout=20, config=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _clear_env(mp):
    for v in ("KB_IPC_COMMAND", "KB_IPC_PYTHONPATH", "KICAD_API_SOCKET"):
        mp.delenv(v, raising=False)


def test_resolve_runner_default_is_kb_ipc_on_path(monkeypatch):
    _clear_env(monkeypatch)
    cmd, pp, sock = sch_live._resolve_runner(_args(), {})
    assert cmd == ["kb-ipc"]
    assert pp is None and sock is None


def test_resolve_runner_env_overrides_config(monkeypatch):
    monkeypatch.setenv("KB_IPC_COMMAND", "/venv/python /x/kb-ipc")
    monkeypatch.setenv("KB_IPC_PYTHONPATH", "/x/kipy")
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///tmp/a.sock")
    ipc = {"command": ["other"], "pythonpath": "/cfg", "socket": "ipc:///cfg.sock"}
    cmd, pp, sock = sch_live._resolve_runner(_args(), ipc)
    assert cmd == ["/venv/python", "/x/kb-ipc"]   # env wins, shlex-split
    assert pp == "/x/kipy"
    assert sock == "ipc:///tmp/a.sock"


def test_resolve_runner_cli_socket_beats_env(monkeypatch):
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///env.sock")
    _, _, sock = sch_live._resolve_runner(_args(socket="ipc:///cli.sock"), {})
    assert sock == "ipc:///cli.sock"


def test_resolve_runner_config_fallback(monkeypatch):
    _clear_env(monkeypatch)
    ipc = {"command": "py /kb-ipc", "pythonpath": "/cfg", "socket": "ipc:///cfg.sock"}
    cmd, pp, sock = sch_live._resolve_runner(_args(), ipc)
    assert cmd == ["py", "/kb-ipc"]               # str config is shlex-split
    assert pp == "/cfg" and sock == "ipc:///cfg.sock"


def test_parser_registers_sch_live():
    parser = build_parser()
    names = set()
    for action in parser._actions:
        if getattr(action, "choices", None):
            names |= set(action.choices.keys())
    assert "sch-live" in names
