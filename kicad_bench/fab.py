"""fab.py — fabrication-export backends behind one seam.

`release-prep` (into the gitignored output/release/) and `release-freeze` (into the
frozen releases/<version>/<board>/) both export fab artifacts; this is the single place
that knows HOW, so a richer engine can be added without touching either caller.

- **KicadCliBackend** (default, ships now) — stdlib + kicad-cli, zero extra deps.
  Gerbers, drill, position, plus STEP and schematic/board PDFs.
- **KibotBackend** (documented, not built) — would add the artifacts kicad-cli can't
  emit (interactive HTML BOM, IPC-D-356, sourced BOM-xlsx). Detected on PATH like
  kicad-cli. See docs/product-workflow-merge.md §4.

Each backend's `export()` returns a `Result`, so a failed artifact fails the freeze via
the usual exit-code contract. Read-first: exports only ever write into the target
out_dir; no design file is touched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

from .core import cli
from .core.report import Result

# Artifact sets. `release-prep` emits the BASIC set into output/release/; a full freeze
# emits FULL into releases/<version>/<board>/.
BASIC = ("gerbers", "drill", "pos")
FULL = ("gerbers", "drill", "pos", "step", "pcb_pdf", "sch_pdf")


class FabBackend(Protocol):
    name: str
    requires: str
    def available(self) -> bool: ...
    def export(self, cfg, out_dir: Path, artifacts: tuple[str, ...] = FULL) -> Result: ...


class KicadCliBackend:
    """Default backend — every artifact kicad-cli can produce headlessly."""
    name = "kicad-cli"
    requires = "kicad-cli on PATH"

    def available(self) -> bool:
        return cli.have_kicad_cli()

    def export(self, cfg, out_dir: Path, artifacts: tuple[str, ...] = FULL) -> Result:
        res = Result(f"Fab export ({self.name})")
        out_dir.mkdir(parents=True, exist_ok=True)
        pcb = str(cfg.pcb) if cfg.pcb else None
        sch = str(cfg.root_sch) if cfg.root_sch else None
        stem = Path(pcb).stem if pcb else "board"

        jobs: list[tuple[list[str], str]] = []
        if pcb:
            if "gerbers" in artifacts:
                jobs.append((["kicad-cli", "pcb", "export", "gerbers",
                              "--output", str(out_dir), pcb], "gerbers"))
            if "drill" in artifacts:
                jobs.append((["kicad-cli", "pcb", "export", "drill",
                              "--output", str(out_dir) + "/", pcb], "drill"))
            if "pos" in artifacts:
                jobs.append((["kicad-cli", "pcb", "export", "pos",
                              "--output", str(out_dir / "positions.csv"),
                              "--format", "csv", pcb], "position file"))
            if "step" in artifacts:
                jobs.append((["kicad-cli", "pcb", "export", "step",
                              "--output", str(out_dir / f"{stem}.step"), pcb], "STEP"))
            if "pcb_pdf" in artifacts:
                jobs.append((["kicad-cli", "pcb", "export", "pdf",
                              "--output", str(out_dir / f"{stem}-pcb.pdf"), pcb], "PCB PDF"))
        if sch and "sch_pdf" in artifacts:
            jobs.append((["kicad-cli", "sch", "export", "pdf",
                          "--output", str(out_dir / f"{Path(sch).stem}-sch.pdf"), sch],
                         "schematic PDF"))

        for cmd, label in jobs:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                res.error(f"{label} export failed", detail=(r.stderr or "").strip()[:200])
            else:
                res.ok(label)
        n_ok = sum(1 for f in res.findings if f.severity == "ok")
        res.summary = f"{n_ok}/{len(jobs)} artifact(s) -> {out_dir}"
        return res


class KibotBackend:
    """Documented future extension — not implemented. Adds ibom / IPC-D-356 / sourced
    BOM-xlsx via KiBot. Reports itself unavailable so `get_backend('kibot')` fails fast
    with a clear message until it lands (see docs/product-workflow-merge.md §4)."""
    name = "kibot"
    requires = "kibot on PATH + a .kibot.yaml (not yet implemented)"

    def available(self) -> bool:
        return False

    def export(self, cfg, out_dir: Path, artifacts: tuple[str, ...] = FULL) -> Result:
        raise NotImplementedError(
            "the KiBot backend is a documented future extension "
            "(docs/product-workflow-merge.md §4)")


_BACKENDS = {"kicad-cli": KicadCliBackend, "kibot": KibotBackend}


def get_backend(name: str = "kicad-cli") -> FabBackend:
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"unknown fab backend {name!r} (have: {', '.join(_BACKENDS)})")
    return cls()
