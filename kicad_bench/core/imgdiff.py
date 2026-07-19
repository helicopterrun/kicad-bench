"""imgdiff.py — pixel diff of two board renders (optional `[imgdiff]` extra).

`board_diff` answers *what* changed structurally. This answers *where* it changed, by
overlaying two revisions of the same board: old-only copper in one colour, new-only in
another, unchanged in grey. On a dense board that lands faster than reading a list of
200 moved refs.

The alignment is free rather than computed: `cli.export_pcb_pdf` uses
`--page-size-mode 2 --exclude-drawing-sheet`, so every render is cropped to the board
outline and two revisions of the same outline land on the same axes. Renders whose
pixel dimensions differ (the board outline itself changed) are reported rather than
stretched to fit — a scaled comparison would paint every trace as moved.

Two renderers, because one of them isn't always available:

* `overlay_svg` composes the two SVG exports directly — stdlib only, no rasterizer, and
  it is the path that works everywhere `kicad-cli pcb export svg` does.
* `overlay` does a true pixel diff and also reports *how much* of the board changed, but
  it needs a raster source. kicad-cli has no 2D PNG export, so it goes board → PDF →
  `pdftoppm` → PNG. **On a flatpak kicad-cli the PDF plotter requires the xdg document
  portal**, which a headless container doesn't have (`Can't get document portal … Can't
  mount path /run/user/0/doc`) — SVG export still works there, PDF and PS do not. So the
  pixel path is for native KiCad installs, and `overlay_svg` is the fallback that always
  runs rather than an inferior second choice.

Pillow is the one heavy dependency here, so it lives behind the `[imgdiff]` extra with a
guarded import, per the dependency-light rule. `overlay_svg` and `board_diff` need
neither Pillow nor poppler.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_INSTALL_HINT = ("the [imgdiff] extra is required for image diffs — "
                 "install with: pip install -e '.[imgdiff]'")


_SVG_OPEN = re.compile(rb"<svg\b[^>]*>", re.I)
_SVG_CLOSE = re.compile(rb"</svg\s*>", re.I)
_HEX = re.compile(rb"#[0-9A-Fa-f]{6}")


def _recolor(svg_body: bytes, rgb: bytes) -> bytes:
    """Force every stroke/fill colour in an SVG fragment to one colour.

    Same trick `cli.export_pcb_layer_svg` uses to tint a single layer: rewrite the hex
    literals. Pure black and pure white are left alone — KiCad uses them for hole
    knockouts and the page background, and recolouring those fills the board solid.
    """
    def sub(m):
        v = m.group(0).upper()
        return m.group(0) if v in (b"#000000", b"#FFFFFF") else rgb
    return _HEX.sub(sub, svg_body)


def overlay_svg(old_svg: bytes, new_svg: bytes,
                old_hex: bytes = b"#FF5050", new_hex: bytes = b"#50C8FF") -> bytes:
    """Stack two board SVGs into one, tinted by revision. Stdlib only.

    Both exports come from `--page-size-mode 2 --exclude-drawing-sheet`, so two
    revisions of the same outline share a viewBox and land pixel-aligned with no
    transform. `mix-blend-mode: screen` makes overlapping copper read as white-ish,
    so unchanged geometry recedes and only-in-one-revision geometry shows in colour.
    """
    head = _SVG_OPEN.search(old_svg)
    if not head or not _SVG_CLOSE.search(old_svg) or not _SVG_OPEN.search(new_svg):
        raise RuntimeError("not a parsable SVG export")

    def body(svg: bytes) -> bytes:
        start = _SVG_OPEN.search(svg).end()
        end = _SVG_CLOSE.search(svg).start()
        return svg[start:end]

    return (head.group(0)
            + b'<rect width="100%" height="100%" fill="#101014"/>'
            + b'<g style="mix-blend-mode:screen">'
            + _recolor(body(old_svg), old_hex) + b"</g>"
            + b'<g style="mix-blend-mode:screen">'
            + _recolor(body(new_svg), new_hex) + b"</g></svg>")


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("pdftoppm"))


def _require():
    try:
        import PIL  # noqa: F401
    except ImportError as e:
        raise RuntimeError(_INSTALL_HINT) from e
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm not found (poppler-utils) — needed to rasterize "
                           "the board PDF for an image diff")


def pdf_to_png(pdf: bytes, dpi: int = 150) -> bytes:
    """First page of a PDF as PNG bytes, via pdftoppm (same tool as the datasheet cache)."""
    _require()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1",
                        str(src), str(Path(td) / "page")],
                       capture_output=True, timeout=120)
        pages = sorted(Path(td).glob("page*.png"))
        if not pages:
            raise RuntimeError("pdftoppm produced no page image")
        return pages[0].read_bytes()


def overlay(old_png: bytes, new_png: bytes,
            old_rgb=(255, 80, 80), new_rgb=(80, 200, 255)) -> tuple[bytes, dict]:
    """Overlay two renders; returns (PNG bytes, stats).

    Anything present in both is drawn dim grey; removed copper takes `old_rgb`, added
    copper `new_rgb`. Stats carry the changed-pixel counts so a caller can say "0.4% of
    the board changed" without looking at the picture.
    """
    _require()
    from PIL import Image, ImageChops

    a = Image.open(io.BytesIO(old_png)).convert("L")
    b = Image.open(io.BytesIO(new_png)).convert("L")
    if a.size != b.size:
        raise RuntimeError(
            f"renders differ in size ({a.size} vs {b.size}) — the board outline itself "
            "changed, so the two revisions cannot be overlaid pixel-for-pixel")

    # Board renders are light-on-dark; treat "has ink" as the signal.
    a_ink = a.point(lambda v: 255 if v > 40 else 0, mode="1")
    b_ink = b.point(lambda v: 255 if v > 40 else 0, mode="1")
    only_old = ImageChops.logical_and(a_ink, ImageChops.invert(b_ink.convert("L")).convert("1"))
    only_new = ImageChops.logical_and(b_ink, ImageChops.invert(a_ink.convert("L")).convert("1"))
    both = ImageChops.logical_and(a_ink, b_ink)

    out = Image.new("RGB", a.size, (16, 16, 20))
    out.paste((90, 90, 96), mask=both)
    out.paste(old_rgb, mask=only_old)
    out.paste(new_rgb, mask=only_new)

    # Pillow 14 renames getdata(); use the histogram instead — it is both stable
    # across versions and far cheaper than materialising every pixel.
    n_old = only_old.convert("L").histogram()[255]
    n_new = only_new.convert("L").histogram()[255]
    total = a.size[0] * a.size[1]
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue(), {
        "removed_px": n_old, "added_px": n_new, "total_px": total,
        "changed_pct": round(100.0 * (n_old + n_new) / total, 3) if total else 0.0,
        "width": a.size[0], "height": a.size[1],
    }
