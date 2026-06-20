"""config.py — load `kicad-bench.toml` and resolve project conventions.

The config is the ONE place a target project's specifics live, so the tools stay
generic. It resolves:
  * file paths (root schematic, PCB, fp-lib-table) relative to the config dir
  * the net-name contract — inline list OR `from:<file>` (a design-rules JSON whose
    `nets`/`diff_pairs` enumerate every named net; or a markdown doc scanned for
    UPPER_SNAKE net tokens)
  * per-block designator ranges (inline; e.g. "R22-R29" expands to R22..R29)
  * ERC allow-rules (known false-positives, each REQUIRING a written reason)
  * the fab/design-rules config path

All paths are returned absolute. Nothing here mutates project files.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "kicad-bench.toml"


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for kicad-bench.toml."""
    start = (start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        cand = d / CONFIG_NAME
        if cand.exists():
            return cand
    return None


@dataclass
class AllowRule:
    """A documented ERC suppression. `reason` is mandatory — an allow without a
    written reason is a config error, not a suppression."""
    reason: str
    type: str = "*"          # ERC violation type, or "*" for any
    ref: str | None = None   # component reference, e.g. "U5"
    pin: str | None = None   # pin number, e.g. "7"
    msg_contains: str | None = None

    def matches(self, vtype: str, msg: str, ref: str | None, pin: str | None) -> bool:
        if self.type not in ("*", vtype):
            return False
        if self.ref is not None and self.ref != ref:
            return False
        if self.pin is not None and str(self.pin) != str(pin):
            return False
        if self.msg_contains is not None and self.msg_contains not in msg:
            return False
        return True


@dataclass
class Config:
    path: Path
    root: Path                              # project root (config dir)
    root_sch: Path | None = None
    pcb: Path | None = None
    fp_lib_table: Path | None = None
    bom: Path | None = None
    nets: set[str] = field(default_factory=set)
    designators: dict[str, set[str]] = field(default_factory=dict)
    expected_nets: dict[str, int] | None = None
    allow_rules: list[AllowRule] = field(default_factory=list)
    fab_config: Path | None = None
    stackup: str | None = None
    raw: dict = field(default_factory=dict)

    def block_for_designator(self, ref: str) -> str | None:
        """Which block reserves this reference designator, if any."""
        for block, refs in self.designators.items():
            if ref in refs:
                return block
        return None

    @property
    def all_reserved(self) -> set[str]:
        out: set[str] = set()
        for refs in self.designators.values():
            out |= refs
        return out


# -- helpers -----------------------------------------------------------------
_RANGE = re.compile(r"^([A-Za-z]+)(\d+)-([A-Za-z]+)?(\d+)$")
_NET_TOKEN = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")


def _expand_designators(spec) -> set[str]:
    """["U4", "R22-R29"] -> {U4, R22, R23, ... R29}."""
    out: set[str] = set()
    items = spec if isinstance(spec, list) else [spec]
    for item in items:
        item = str(item).strip()
        if item in ("", "—", "-"):
            continue
        m = _RANGE.match(item)
        if m:
            pre, lo, pre2, hi = m.groups()
            if pre2 and pre2 != pre:
                out.add(item)            # weird, keep verbatim
                continue
            for n in range(int(lo), int(hi) + 1):
                out.add(f"{pre}{n}")
        else:
            out.add(item)
    return out


def _resolve_path(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def _load_nets_from_file(path: Path) -> set[str]:
    """Pull net names from a design-rules JSON or, failing that, a markdown doc."""
    if not path.exists():
        sys.exit(f"error: net source not found: {path}")
    if path.suffix == ".json":
        cfg = json.loads(path.read_text())
        nets: set[str] = set()
        for group in cfg.get("nets", {}).values():
            nets |= set(group)
        for spec in cfg.get("diff_pairs", {}).values():
            for base in spec.get("members", []):
                nets |= {f"{base}_P", f"{base}_N", f"{base}P", f"{base}N"}
        return nets
    # markdown / text: best-effort scan for UPPER_SNAKE tokens
    return set(_NET_TOKEN.findall(path.read_text()))


def load(config_path: Path) -> Config:
    data = tomllib.loads(config_path.read_text())
    proj = data.get("project", {})
    # Project root defaults to the config's own directory, but a config kept in a
    # separate tooling repo can point at the design repo via project.root.
    root = config_path.resolve().parent
    if "root" in proj:
        rp = Path(proj["root"])
        root = rp if rp.is_absolute() else (root / rp).resolve()
    cfg = Config(path=config_path.resolve(), root=root, raw=data)

    for attr, key in (("root_sch", "root_sch"), ("pcb", "pcb"),
                      ("fp_lib_table", "fp_lib_table"), ("bom", "bom")):
        if key in proj:
            setattr(cfg, attr, _resolve_path(root, proj[key]))

    contracts = data.get("contracts", {})

    nets_spec = contracts.get("nets")
    if isinstance(nets_spec, list):
        cfg.nets = set(nets_spec)
    elif isinstance(nets_spec, str) and nets_spec.startswith("from:"):
        cfg.nets = _load_nets_from_file(_resolve_path(root, nets_spec[len("from:"):]))

    for block, spec in contracts.get("designators", {}).items():
        cfg.designators[block] = _expand_designators(spec)

    exp = contracts.get("expected_nets")
    if exp:
        ep = _resolve_path(root, exp)
        if ep.exists():
            cfg.expected_nets = json.loads(ep.read_text())

    for entry in data.get("erc", {}).get("allow", []):
        if "reason" not in entry:
            sys.exit(
                f"error: erc.allow entry without a 'reason' in {config_path} — "
                "every suppression must be justified."
            )
        cfg.allow_rules.append(AllowRule(
            reason=entry["reason"],
            type=entry.get("type", "*"),
            ref=entry.get("ref"),
            pin=entry.get("pin"),
            msg_contains=entry.get("msg_contains"),
        ))

    fab = data.get("fab", {})
    if "config" in fab:
        cfg.fab_config = _resolve_path(root, fab["config"])
    cfg.stackup = fab.get("stackup")

    return cfg


def load_or_exit(explicit: str | None) -> Config:
    """Resolve the config from --config or by upward search; exit(2) if absent."""
    if explicit:
        p = Path(explicit)
        if not p.exists():
            sys.exit(f"error: config not found: {p}")
        return load(p)
    found = find_config()
    if not found:
        sys.exit(
            f"error: no {CONFIG_NAME} found in this or any parent directory. "
            "Pass --config PATH or add one (see kicad-bench README)."
        )
    return load(found)
