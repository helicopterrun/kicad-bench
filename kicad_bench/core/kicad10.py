"""kicad10.py — reusable KiCad-10 file-format helpers.

Lifted from the project's `sheetgen.py` "hard lessons" so any future *writer*
reuses them instead of re-learning the trap: kicad-sch-api 0.5.x writes KiCad-9
version markers (`20250114` / "9.0"), which KiCad 10 silently rejects, replacing
the file with an empty skeleton. Any tool that emits a .kicad_sch must post-patch
to the KiCad-10 markers; paper size must likewise be patched to the ANSI code the
API whitelist rejects.

These are pure-string transforms — they do NOT touch schematic design content
(symbols, wires, nets). kicad-bench tools are read-first; this module exists for
the layout artifacts (and any future writers) that legitimately emit KiCad files.
"""
from __future__ import annotations

import re
from pathlib import Path

KICAD10_VERSION = "20260306"   # CLAUDE.md "Hard lesson" 1
KICAD10_GENVER = "10.0"


def patch_version(text: str) -> str:
    """Rewrite the top-of-file (version …)/(generator_version …) to KiCad 10."""
    text = re.sub(r"\(version\s+\d+\)", f"(version {KICAD10_VERSION})", text, count=1)
    text = re.sub(
        r'\(generator_version\s+"[^"]*"\)',
        f'(generator_version "{KICAD10_GENVER}")',
        text, count=1,
    )
    return text


def patch_paper(text: str, paper: str = "B") -> str:
    """Force the paper code (the API whitelist rejects single-letter ANSI codes)."""
    return re.sub(r'\(paper\s+"[^"]*"\)', f'(paper "{paper}")', text, count=1)


def patch_file(path: str | Path, paper: str | None = "B") -> None:
    """Post-patch a freshly written .kicad_sch/.kicad_pcb in place."""
    p = Path(path)
    text = patch_version(p.read_text())
    if paper is not None:
        text = patch_paper(text, paper)
    p.write_text(text)
