"""kicad-bench `kb` CLI entry point.

Dispatches to the quality-gate (Priority 1) and layout/DFM (Priority 2) tools.
Each tool module exposes `add_parser(subparsers)` registering its subcommand and a
`run(args)` returning a process exit code.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, audit, datasheet, libsearch, release_freeze, sch_live, scaffold, sidecar, stage
from . import init as init_cmd
from .core import cli
from .quality import (approved_parts, block_review, cap_derating, commit_gate,
                      erc_triage, ksir_sync, led_current, netlist_audit,
                      pin_mux, symbol_style, sch_readability)
from .layout import (dfm_preflight, diffpair_audit, dru_guard, dru_lint,
                     netclass_coverage, netclass_sync, release_prep,
                     route_coverage, stackup_sync, track_conformance)

_TOOLS = [
    scaffold, release_freeze,                                    # Priority 0: new product monorepo + release freeze
    init_cmd,                                                    # one-command bring-up for a new board
    audit, sidecar, stage, datasheet,                            # consolidated audit + live web sidecar + lock-aware queue + datasheet viewer
    sch_live,                                                    # live schematic summary over the IPC API (shells to kb-ipc add-on)
    libsearch,                                                   # FTS over installed symbol/footprint libs
    erc_triage, netlist_audit, block_review, approved_parts, commit_gate,  # Priority 1
    ksir_sync,                                                   # .ksir <-> sheet drift gate
    symbol_style, sch_readability,                               # library + sheet layout rule gates
    cap_derating, led_current, pin_mux,                          # datasheet-grounded deterministic checks
    netclass_coverage, netclass_sync, dfm_preflight, stackup_sync,  # Priority 2
    release_prep, dru_lint,
    diffpair_audit, track_conformance, route_coverage, dru_guard,   # Priority 3: routed-geometry audits
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kb",
        description="kicad-bench — generic KiCad-10 workflow & PCB-design workbench.",
    )
    p.add_argument("--version", action="version", version=f"kicad-bench {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    for tool in _TOOLS:
        tool.add_parser(sub)
    # tiny diagnostics command
    d = sub.add_parser("doctor", help="check the environment (kicad-cli, config)")
    d.add_argument("--config", help="path to kicad-bench.toml")
    d.set_defaults(func=_doctor)
    return p


def _doctor(args) -> int:
    from .core import config as cfgmod
    ok = True
    if cli.have_kicad_cli():
        print(f"✓ kicad-cli: {cli.version()}")
    else:
        print("✗ kicad-cli not found on PATH")
        ok = False
    resolved = cfgmod.resolve_config_path(args.config)
    if args.config:
        print(f"· config (explicit): {args.config}")
    elif resolved:
        via = "$KICAD_BENCH_CONFIG" if cfgmod.os.environ.get(cfgmod.ENV_VAR) else \
              ("pointer" if cfgmod.find_pointer() else "upward search")
        print(f"✓ config: {resolved}  (via {via})")
    else:
        print("· no config found (run `kb init <repo>` or pass --config)")
    return 0 if ok else 2


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except BrokenPipeError:
        # A downstream consumer (e.g. `kb datasheet search … | head`) closed the pipe.
        # Exit quietly like coreutils: redirect stdout to devnull so the interpreter's
        # flush-on-exit doesn't re-raise BrokenPipeError as an ugly traceback.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
