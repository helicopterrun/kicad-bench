"""cli.py — one wrapper around `kicad-cli` with consistent error handling.

All KiCad interaction in kicad-bench goes through here so the flatpak-wrapper
caveats (private /tmp, output paths) live in exactly one place, and so a missing
or wrong-version kicad-cli produces a single clear error instead of N stack traces.

Reference: the project's `kicad-cli` is a flatpak wrapper that passes
`--filesystem=/tmp`, so NamedTemporaryFile output works. See the project
toolchain-setup notes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


class KicadCliError(RuntimeError):
    pass


def have_kicad_cli() -> bool:
    return shutil.which("kicad-cli") is not None


def version() -> str:
    _require()
    r = subprocess.run(["kicad-cli", "version"], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def _require() -> None:
    if not have_kicad_cli():
        raise KicadCliError(
            "kicad-cli not found on PATH. Install KiCad 10 (kicad-cli) — see the "
            "project toolchain notes."
        )


def erc_report(sch: str | Path) -> str:
    """Run severity-error ERC and return the raw report text."""
    _require()
    with tempfile.NamedTemporaryFile(suffix=".rpt", delete=False) as f:
        rpt = f.name
    subprocess.run(
        ["kicad-cli", "sch", "erc", "--severity-error", "--output", rpt, str(sch)],
        capture_output=True, text=True,
    )
    text = Path(rpt).read_text()
    Path(rpt).unlink(missing_ok=True)
    return text


def netlist_xml(sch: str | Path) -> ET.Element:
    """Export the kicadxml netlist and return its parsed root element."""
    _require()
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
        xml = f.name
    r = subprocess.run(
        ["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
         "--output", xml, str(sch)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        Path(xml).unlink(missing_ok=True)
        raise KicadCliError(f"netlist export failed:\n{r.stderr.strip()}")
    root = ET.parse(xml).getroot()
    Path(xml).unlink(missing_ok=True)
    return root


def netlist_nets(sch: str | Path) -> tuple[dict[str, list[str]], dict[tuple[str, str], str]]:
    """Return (counts, pin_net):

      counts  : net name (leading '/' stripped) -> ["ref.pin", ...]
      pin_net : (ref, pin) -> net name

    Mirrors validate_sheet.export_netlist so the audit/triage logic is identical.
    """
    root = netlist_xml(sch)
    counts: dict[str, list[str]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    for net in root.iter("net"):
        name = (net.get("name") or "").lstrip("/")
        nodes = net.findall("node")
        counts[name] = [f"{n.get('ref')}.{n.get('pin')}" for n in nodes]
        for n in nodes:
            pin_net[(n.get("ref"), n.get("pin"))] = name
    return counts, pin_net


def drc_json(pcb: str | Path) -> dict:
    """Run DRC with structured JSON output and return the parsed result.

    Custom-rule loading (verified empirically on KiCad 10.0.3): `kicad-cli pcb drc`
    DOES apply a project's custom design rules, but only from a file named
    `<board>.kicad_dru` sitting NEXT TO the board (matching base name). There is no
    CLI flag to point at an arbitrary path, and a DRU with a syntax error is
    silently dropped (rules simply don't apply). Consequence for this project: the
    rules emitted by gen_design_rules.py land in output/ (gitignored), so they are
    NOT active until copied to `<project>.kicad_dru` next to the board.
    Also note `--severity-error` filters the report to error-severity items only.
    """
    _require()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = f.name
    r = subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all",
         "--output", out, str(pcb)],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(Path(out).read_text())
    except (OSError, json.JSONDecodeError):
        raise KicadCliError(f"DRC produced no parseable report:\n{r.stderr.strip()}")
    finally:
        Path(out).unlink(missing_ok=True)
    return data


def drc_violations(pcb: str | Path, severities=("error",)) -> list[dict]:
    """Return DRC violations at the given severities (each: type/severity/description)."""
    want = set(severities)
    return [v for v in drc_json(pcb).get("violations", []) if v.get("severity") in want]
