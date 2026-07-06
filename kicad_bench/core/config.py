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

Single-board is the default: `[project]` synthesizes one implicit board. A product
monorepo declares several via `[[boards]]` (optionally under a `[product]` wrapper), each
inheriting the product-level `[contracts]`/`[fab]` unless it overrides them. The active
board's fields are mirrored onto the top-level Config, so every command keeps reading
`cfg.root_sch`/`cfg.pcb`/… unchanged.

All paths are returned absolute. Nothing here mutates project files.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "kicad-bench.toml"
POINTER_NAME = ".kicad-bench-config"   # in-repo file naming where the config lives
ENV_VAR = "KICAD_BENCH_CONFIG"


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for kicad-bench.toml."""
    start = (start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        cand = d / CONFIG_NAME
        if cand.exists():
            return cand
    return None


def find_pointer(start: Path | None = None) -> Path | None:
    """Walk up from `start` for a `.kicad-bench-config` pointer file and return the
    config it names (first non-comment line; path resolved relative to the pointer).

    This is how a design repo whose config lives elsewhere (e.g. in the kicad-bench
    repo's configs/) advertises that location without an env var. Returns None if no
    pointer is found or the named config doesn't exist.
    """
    start = (start or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        ptr = d / POINTER_NAME
        if not ptr.exists():
            continue
        for line in ptr.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = Path(line)
            p = p if p.is_absolute() else (d / p)
            return p.resolve() if p.exists() else None
        return None
    return None


def resolve_config_path(explicit: str | None = None, start: Path | None = None) -> Path | None:
    """Resolve which kicad-bench.toml to use, in priority order:

      1. an explicit --config path,
      2. the $KICAD_BENCH_CONFIG env var (if it points at an existing file),
      3. a `.kicad-bench-config` pointer file found walking up from cwd,
      4. a `kicad-bench.toml` found walking up from cwd.

    Returns None only if nothing is found (and no explicit path was given).
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get(ENV_VAR)
    if env and Path(env).exists():
        return Path(env).resolve()
    ptr = find_pointer(start)
    if ptr:
        return ptr
    return find_config(start)


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
class SidecarTab:
    """An extra HTML page the sidecar serves as a tab (project docs/guides).
    `path` is absolute; resolved relative to the project root."""
    title: str
    path: Path


@dataclass
class BoardConfig:
    """One board's resolved view. In a single-board config there is exactly one of
    these (synthesized from `[project]`); a product-monorepo config declares several
    via `[[boards]]`. Its fields mirror the top-level `Config` fields every command
    already reads — the active board is copied onto `Config` so nothing downstream has
    to change."""
    name: str
    root_sch: Path | None = None
    pcb: Path | None = None
    fp_lib_table: Path | None = None
    bom: Path | None = None
    nets: set[str] = field(default_factory=set)
    designators: dict[str, set[str]] = field(default_factory=dict)
    expected_nets: dict[str, int] | None = None
    expected_nets_path: Path | None = None
    allow_rules: list[AllowRule] = field(default_factory=list)
    fab_config: Path | None = None
    stackup: str | None = None


@dataclass
class ProductConfig:
    """Optional `[product]` section — the multi-board monorepo wrapper. `firmware`,
    `mechanical`, and `releases` are absolute dirs (releases defaults to
    `<root>/releases`)."""
    name: str
    root: Path
    firmware: Path | None = None
    mechanical: Path | None = None
    releases: Path | None = None


@dataclass
class ApprovedParts:
    """Optional `[approved_parts]` section — the MPN allow-list governance config. The
    `kb approved-parts` gate (a later phase) consumes this; here we only parse it."""
    csv: Path
    allow_missing_mpn: bool = False
    unapproved: str = "warn"   # "warn" | "error" for an MPN not on the list


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
    expected_nets_path: Path | None = None
    allow_rules: list[AllowRule] = field(default_factory=list)
    fab_config: Path | None = None
    stackup: str | None = None
    sidecar_tabs: list[SidecarTab] = field(default_factory=list)
    product: ProductConfig | None = None
    boards: list[BoardConfig] = field(default_factory=list)
    active_board: str | None = None
    approved_parts: ApprovedParts | None = None
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

    def board(self, name: str) -> BoardConfig | None:
        """Look up a declared board by name."""
        for b in self.boards:
            if b.name == name:
                return b
        return None

    def _apply_active(self, b: BoardConfig) -> None:
        """Mirror a board's fields onto the top-level Config fields. This is the
        backward-compat pivot: every command reads `cfg.root_sch`/`cfg.pcb`/… and
        transparently operates on whichever board is active."""
        self.active_board = b.name
        self.root_sch = b.root_sch
        self.pcb = b.pcb
        self.fp_lib_table = b.fp_lib_table
        self.bom = b.bom
        self.nets = b.nets
        self.designators = b.designators
        self.expected_nets = b.expected_nets
        self.expected_nets_path = b.expected_nets_path
        self.allow_rules = b.allow_rules
        self.fab_config = b.fab_config
        self.stackup = b.stackup

    def activate(self, name: str) -> None:
        """Make `name` the active board (re-point the top-level fields). exit(2) on an
        unknown board — a ready hook for a future `--board` selector."""
        b = self.board(name)
        if b is None:
            avail = ", ".join(x.name for x in self.boards) or "(none)"
            sys.exit(f"error: no board named {name!r} in {self.path} — available: {avail}")
        self._apply_active(b)


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


def _parse_allow_rules(data: dict, config_path: Path) -> list[AllowRule]:
    """Product-wide ERC allow-rules from `[[erc.allow]]`. Each requires a reason."""
    rules: list[AllowRule] = []
    for entry in data.get("erc", {}).get("allow", []):
        if "reason" not in entry:
            sys.exit(
                f"error: erc.allow entry without a 'reason' in {config_path} — "
                "every suppression must be justified."
            )
        rules.append(AllowRule(
            reason=entry["reason"],
            type=entry.get("type", "*"),
            ref=entry.get("ref"),
            pin=entry.get("pin"),
            msg_contains=entry.get("msg_contains"),
        ))
    return rules


def _resolve_board(name: str, src: dict, root: Path, config_dir: Path,
                   p_contracts: dict, p_fab: dict,
                   allow_rules: list[AllowRule]) -> BoardConfig:
    """Build one BoardConfig. `src` carries the board's own path keys (and optional
    `contracts`/`fab` subtables); a `[[boards]]` entry overrides the product-level
    `p_contracts`/`p_fab` per key, else inherits them. For the implicit single-board
    case `src` is the `[project]` table, so the resolved board is byte-identical to the
    pre-multi-board behaviour."""
    b = BoardConfig(name=name, allow_rules=allow_rules)

    for attr, key in (("root_sch", "root_sch"), ("pcb", "pcb"),
                      ("fp_lib_table", "fp_lib_table"), ("bom", "bom")):
        if key in src:
            setattr(b, attr, _resolve_path(root, src[key]))

    # Board `contracts`/`fab` override the product-level tables per top-level key.
    contracts = {**p_contracts, **src.get("contracts", {})}
    fab = {**p_fab, **src.get("fab", {})}

    nets_spec = contracts.get("nets")
    if isinstance(nets_spec, list):
        b.nets = set(nets_spec)
    elif isinstance(nets_spec, str) and nets_spec.startswith("from:"):
        b.nets = _load_nets_from_file(_resolve_path(root, nets_spec[len("from:"):]))

    for block, spec in contracts.get("designators", {}).items():
        b.designators[block] = _expand_designators(spec)

    # expected_nets is a tooling artifact (a generated baseline), so it resolves
    # relative to the CONFIG file's directory — not the design-repo root.
    exp = contracts.get("expected_nets")
    if exp:
        b.expected_nets_path = _resolve_path(config_dir, exp)
        if b.expected_nets_path.exists():
            b.expected_nets = json.loads(b.expected_nets_path.read_text())

    if "config" in fab:
        b.fab_config = _resolve_path(root, fab["config"])
    b.stackup = fab.get("stackup")
    return b


def load(config_path: Path) -> Config:
    data = tomllib.loads(config_path.read_text())
    config_dir = config_path.resolve().parent
    proj = data.get("project", {})
    # Project root defaults to the config's own directory, but a config kept in a
    # separate tooling repo can point at the design repo via project.root.
    root = config_dir
    if "root" in proj:
        rp = Path(proj["root"])
        root = rp if rp.is_absolute() else (root / rp).resolve()
    cfg = Config(path=config_path.resolve(), root=root, raw=data)

    # Product-level defaults — boards inherit these unless they override.
    p_contracts = data.get("contracts", {})
    p_fab = data.get("fab", {})
    allow_rules = _parse_allow_rules(data, config_path)

    # Optional [product] wrapper (the multi-board monorepo).
    prod = data.get("product")
    if prod:
        cfg.product = ProductConfig(
            name=prod.get("name", root.name),
            root=root,
            firmware=_resolve_path(root, prod["firmware"]) if "firmware" in prod else None,
            mechanical=_resolve_path(root, prod["mechanical"]) if "mechanical" in prod else None,
            releases=_resolve_path(root, prod.get("releases", "releases")),
        )

    # Boards: explicit [[boards]], else one implicit board synthesized from [project].
    board_entries = data.get("boards")
    if board_entries:
        for entry in board_entries:
            name = entry.get("name") or "main"
            cfg.boards.append(_resolve_board(
                name, entry, root, config_dir, p_contracts, p_fab, allow_rules))
    else:
        name = (cfg.product.name if cfg.product else None) or "main"
        cfg.boards.append(_resolve_board(
            name, proj, root, config_dir, p_contracts, p_fab, allow_rules))

    # Active board = the first declared; its fields mirror onto the top-level Config,
    # so every existing command keeps working with zero changes.
    cfg._apply_active(cfg.boards[0])

    # Sidecar doc tabs — project HTML guides shown alongside the audit dashboard.
    # `file` resolves relative to the project root (they live with the design).
    for tab in data.get("sidecar", {}).get("tabs", []):
        title, fname = tab.get("title"), tab.get("file")
        if title and fname:
            cfg.sidecar_tabs.append(SidecarTab(title=title, path=_resolve_path(root, fname)))

    # Optional [approved_parts] — MPN allow-list governance (gate lands in a later phase).
    ap = data.get("approved_parts")
    if ap and ap.get("csv"):
        cfg.approved_parts = ApprovedParts(
            csv=_resolve_path(root, ap["csv"]),
            allow_missing_mpn=bool(ap.get("allow_missing_mpn", False)),
            unapproved=ap.get("unapproved", "warn"),
        )

    return cfg


def load_or_exit(explicit: str | None, board: str | None = None) -> Config:
    """Resolve the config (--config, $KICAD_BENCH_CONFIG, .kicad-bench-config pointer,
    or upward search for kicad-bench.toml) and load it; exit(2) if absent. An optional
    `board` name activates that board — a ready hook for a future `--board` selector."""
    if explicit and not Path(explicit).exists():
        sys.exit(f"error: config not found: {explicit}")
    p = resolve_config_path(explicit)
    if not p or not p.exists():
        sys.exit(
            f"error: no {CONFIG_NAME} found via --config, ${ENV_VAR}, a "
            f"{POINTER_NAME} pointer, or upward search. Run `kb init <repo>` or pass "
            "--config PATH (see kicad-bench README)."
        )
    cfg = load(p)
    if board is not None:
        cfg.activate(board)
    return cfg
