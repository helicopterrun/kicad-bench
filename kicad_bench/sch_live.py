"""sch-live — live SCHEMATIC summary from a running KiCad over the IPC API.

KiCad master (→ v11) serves the eeschema IPC API; the `kb-ipc` add-on (a separate
repo, with the `kipy` client) talks to it. This command keeps `kb` stdlib-only and
read-only by *shelling out* to `kb-ipc sch --json` — exactly as the rest of kb shells
out to `kicad-cli` — and reporting the live counts. It never edits a design file; the
live *write* path stays in `kb-ipc sch-apply` by design (kb's read-only boundary).

How `kb-ipc` is invoked is resolved in priority order so a COMMITTED kicad-bench.toml
stays portable (zero machine-absolute paths):

  1. env vars (preferred for machine-specific bits — set per host, not committed):
       KB_IPC_COMMAND      e.g. "/root/kicad-python/.venv/bin/python /root/kicad-ipc-plugin/kb-ipc"
       KB_IPC_PYTHONPATH   e.g. "/root/kicad-python"   (prepended to the subprocess PYTHONPATH)
       KICAD_API_SOCKET    e.g. "ipc:///tmp/kicad/test.sock"
  2. an optional, non-portable `[ipc]` config section (command / pythonpath / socket).
  3. defaults: `kb-ipc` on PATH (the normal install: Mac GUI box with kicad-python pip-installed).

Read-only. Exits 0 on a successful read, 2 if it can't reach a running api-server.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from .core import config as cfgmod
from .core.report import Result, render_and_exit

_ORDER = ["top_sheets", "symbols", "sheet_symbols", "labels", "wires", "texts", "nets"]


def _die(msg: str):
    """Print an error and exit 2 (kb's ERROR code: 'could not run')."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _ipc_settings(args) -> dict:
    """Resolve the optional [ipc] config (config is not required for sch-live)."""
    if args.config and not os.path.exists(args.config):
        _die(f"error: config not found: {args.config}")
    p = cfgmod.resolve_config_path(args.config)
    if p and p.exists():
        return cfgmod.load(p).raw.get("ipc", {}) or {}
    return {}


def _resolve_runner(args, ipc: dict):
    """Resolve (command, pythonpath, socket): env vars first (machine-specific, keeps
    any committed config portable), then config [ipc], then `kb-ipc` on PATH."""
    cmd_src = os.environ.get("KB_IPC_COMMAND") or ipc.get("command") or ["kb-ipc"]
    cmd = shlex.split(cmd_src) if isinstance(cmd_src, str) else list(cmd_src)
    pp = os.environ.get("KB_IPC_PYTHONPATH") or ipc.get("pythonpath")
    socket = args.socket or os.environ.get("KICAD_API_SOCKET") or ipc.get("socket")
    return cmd, pp, socket


def run(args) -> int:
    ipc = _ipc_settings(args)
    cmd, pp, socket = _resolve_runner(args, ipc)

    env = os.environ.copy()
    if pp:
        parts = pp if isinstance(pp, list) else [pp]
        env["PYTHONPATH"] = os.pathsep.join(
            [*parts, env.get("PYTHONPATH", "")]).strip(os.pathsep)
    if socket:
        env["KICAD_API_SOCKET"] = socket

    full = [*cmd, "sch", "--json"]
    try:
        r = subprocess.run(full, capture_output=True, text=True, env=env,
                           timeout=args.timeout)
    except FileNotFoundError:
        _die(f"error: kb-ipc not found ({cmd[0]!r}). Set KB_IPC_COMMAND or "
             f"[ipc].command in {cfgmod.CONFIG_NAME} (see `kb sch-live --help`).")
    except subprocess.TimeoutExpired:
        _die(f"error: kb-ipc timed out after {args.timeout}s — is the KiCad "
             "schematic api-server running?")

    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        _die("error: kb-ipc sch failed (is a schematic api-server running, "
             f"KiCad master/11?):\n  {msg}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        _die(f"error: kb-ipc returned non-JSON:\n{r.stdout[:400]}")

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    res = Result("Live schematic (IPC)")
    for k in _ORDER:
        if data.get(k) is not None:
            res.info(f"{k}: {data[k]}", k)
    res.summary = (f"{data.get('symbols', '?')} symbols, {data.get('nets', '?')} nets, "
                   f"{data.get('labels', '?')} labels (live over IPC)")
    return render_and_exit(res)


def add_parser(sub):
    p = sub.add_parser("sch-live",
                       help="live schematic summary from a running KiCad (IPC API)")
    p.add_argument("--socket", help="override API socket (ipc://… ); else config / "
                                    "KICAD_API_SOCKET / kb-ipc discovery")
    p.add_argument("--json", action="store_true", help="emit the raw live summary JSON")
    p.add_argument("--timeout", type=int, default=20,
                   help="seconds to wait for kb-ipc (default 20)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
