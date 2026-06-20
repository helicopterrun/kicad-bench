"""kicad-bench — generic KiCad-10 workflow & PCB-design workbench.

Read-first helpers (ERC triage, netlist audit, block review, commit gate) plus
layout/DFM prep, driven by a per-project `kicad-bench.toml`. Nothing here edits a
schematic design file; see README.md for the read/write boundary.
"""

__version__ = "0.1.0"
