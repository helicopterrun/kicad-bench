"""pin-mux — pin-mux feasibility check (read-only).

When a net's *name* asserts a peripheral signal ("UART5_TX", "/I2C0.SCL") and it
lands on an IC pin whose alternate-function table exposes that peripheral but NOT
that signal (e.g. the pin offers UART5 only as RX), the assignment is physically
unrealizable — a hard, context-free error caught at the schematic.

This is strictly a FEASIBILITY check, never a DIRECTION check: it makes no claim
about whether a TX should meet a peer's RX (crossover) or TX (straight-through to a
transceiver). To stay sound it skips any net where another component's pin table
exposes the same peripheral — an inter-device link where the net name's perspective
is ambiguous.

Alternate-function tables come from the extracted per-part constraints store; with
no tables ingested the check reports one info line and passes.

Idea from Pinscope's pin-mux feasibility check (implementation is original).
Read-only. Exit 0/1/2 per kb convention.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ..core import config as cfgmod, graph as graphmod
from ..core.report import Result, render_and_exit

# ref -> {pin number -> [alternate function strings]}
PinTables = dict[str, dict[str, list[str]]]

_PERIPHERALS = ("LPUART", "USART", "UART", "QSPI", "SPI", "I2C", "I2S",
                "CAN", "SDMMC", "SDIO", "PWM", "TIM")
_SIGNALS = ("MOSI", "MISO", "SCLK", "SCK", "CLK", "NSS", "CS", "SS",
            "SDA", "SCL", "TX", "RX", "RTS", "CTS", "WS", "MCK", "SD")
_ALIASES = {"SCLK": "SCK", "CLK": "SCK", "NSS": "CS", "SS": "CS"}

# Applied to `_norm`-alized text (A-Z0-9 and "_" only), so word boundaries are "_",
# start, or end — `\b` would not fire between "_" and a letter.
_TOKEN = re.compile(
    r"(?:^|_)(" + "|".join(_PERIPHERALS) + r")(\d*)_?("
    + "|".join(_SIGNALS) + r")(?:_|$)")


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", s.upper()).strip("_")


def parse_net_token(net_name: str) -> tuple[str, str] | None:
    """("UART5", "TX") when the net name asserts a peripheral signal, else None."""
    m = _TOKEN.search(_norm(net_name.rsplit("/", 1)[-1]))
    if not m:
        return None
    periph, idx, signal = m.groups()
    return f"{periph}{idx}", _ALIASES.get(signal, signal)


def _exposed_signals(functions: list[str], peripheral: str) -> set[str]:
    """Signals a pin exposes for `peripheral` per its alternate-function list."""
    out: set[str] = set()
    for fn in functions:
        m = _TOKEN.search(_norm(fn))
        if m:
            periph, idx, signal = m.groups()
            if f"{periph}{idx}" == peripheral:
                out.add(_ALIASES.get(signal, signal))
    return out


def _peer_exposes(g: graphmod.DesignGraph, tables: PinTables,
                  net_name: str, ref: str, peripheral: str) -> bool:
    """True when another component on this net also exposes `peripheral`."""
    net = g.nets.get(net_name)
    if not net:
        return False
    for peer_ref, peer_pin in net.pins:
        if peer_ref == ref:
            continue
        funcs = tables.get(peer_ref, {}).get(peer_pin, [])
        if funcs and _exposed_signals(funcs, peripheral):
            return True
    return False


def check(g: graphmod.DesignGraph, tables: PinTables) -> Result:
    """Pure feasibility check over a built design graph + alt-function tables."""
    res = Result("Pin-mux feasibility")
    if not tables:
        res.info("no pin tables available — ingest constraints (kb constraints) "
                 "to enable this check")
        res.summary = "0 checked (no pin tables)"
        return res

    checked = infeasible = 0
    for ref in sorted(tables):
        comp = g.components.get(ref)
        if not comp:
            continue
        table = tables[ref]
        for pin, net_name in sorted(comp.pins.items()):
            token = parse_net_token(net_name)
            funcs = table.get(pin, [])
            if token is None or not funcs:
                continue
            peripheral, signal = token
            exposed = _exposed_signals(funcs, peripheral)
            if not exposed:
                continue        # pin doesn't expose this peripheral at all — not our case
            checked += 1
            if signal in exposed:
                continue        # feasible; direction questions are for the reviewer
            if _peer_exposes(g, tables, net_name, ref, peripheral):
                continue        # inter-device link — net-name perspective is ambiguous
            infeasible += 1
            res.error(f"{ref}.{pin} on net {net_name!r} — pin exposes {peripheral} "
                      f"only as {'/'.join(sorted(exposed))}, not {signal}",
                      f"{ref}.{pin}",
                      "the asserted function cannot be muxed to this pin")

    if checked and not infeasible:
        res.ok(f"all {checked} asserted peripheral pin(s) feasible")
    res.summary = (f"{checked} asserted pin(s) checked against "
                   f"{len(tables)} pin table(s), {infeasible} infeasible")
    return res


def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    sch = Path(args.schematic) if args.schematic else cfg.root_sch
    if not sch or not Path(sch).exists():
        sys.exit(f"error: schematic not found: {sch}")
    g = graphmod.build(sch)
    return render_and_exit(check(g, tables_from_cfg(cfg, g)))


def tables_from_cfg(cfg: cfgmod.Config, g: graphmod.DesignGraph) -> PinTables:
    """Alt-function tables from the extracted-constraints store (kb constraints)."""
    from ..core import conspec
    return conspec.pin_tables(cfg, g.components)


def add_parser(sub):
    p = sub.add_parser("pin-mux",
                       help="pin-mux feasibility (net-name asserted signals vs alt-function tables)")
    p.add_argument("schematic", nargs="?",
                   help="root sheet (default: project.root_sch / active board)")
    p.add_argument("--board", help="which board (multi-board configs; default: first)")
    p.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
