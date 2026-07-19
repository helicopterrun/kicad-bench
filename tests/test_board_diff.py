"""Tests for board-diff — structural PCB delta + the git read path (pure/local)."""
import subprocess

from kicad_bench.core import pcbgeom
from kicad_bench.layout import board_diff

BASE = """\
(kicad_pcb (version 20260306) (generator "pcbnew")
  (net 0 "")
  (net 1 "/VCC")
  (segment (start 0 0) (end 2 0) (width 0.2) (layer "F.Cu") (net "/VCC") (uuid "a"))
  (footprint "lib:R_0402" (property "Reference" "R1") (at 10 10 0) (layer "F.Cu")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "/VCC"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 0 "")))
  (footprint "lib:C_0402" (property "Reference" "C1") (at 20 20 0) (layer "F.Cu")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "/VCC")))
)
"""


def _b(tmp_path, text, name="b.kicad_pcb"):
    p = tmp_path / name
    p.write_text(text)
    return pcbgeom.parse(p)


def test_identical_boards_report_no_change(tmp_path):
    a = _b(tmp_path, BASE, "a.kicad_pcb")
    b = _b(tmp_path, BASE, "b.kicad_pcb")
    delta = board_diff.diff_boards(a, b)
    assert not any(delta[k] for k in ("added_refs", "removed_refs", "moved"))
    res = board_diff.summarize(delta, "x", "y")
    assert res.summary == "no change"


def test_file_churn_without_structural_change_says_so(tmp_path):
    """A re-pour rewrites megabytes and moves nothing — must not read as 'no change'."""
    a = _b(tmp_path, BASE, "a.kicad_pcb")
    b = _b(tmp_path, BASE, "b.kicad_pcb")
    res = board_diff.summarize(board_diff.diff_boards(a, b), "x", "y", file_changed=True)
    assert "no structural change" in res.summary
    assert "file differs" in res.summary


def test_moved_component_reports_offset(tmp_path):
    moved = BASE.replace('(property "Reference" "R1") (at 10 10 0)',
                         '(property "Reference" "R1") (at 12.5 8 0)')
    delta = board_diff.diff_boards(_b(tmp_path, BASE, "a.kicad_pcb"),
                                   _b(tmp_path, moved, "b.kicad_pcb"))
    assert delta["moved"] == [{"ref": "R1", "dx": 2.5, "dy": -2.0,
                               "drot": 0.0, "flipped": False}]


def test_sub_tolerance_move_is_not_reported(tmp_path):
    nudged = BASE.replace("(at 10 10 0)", "(at 10.001 10 0)")
    delta = board_diff.diff_boards(_b(tmp_path, BASE, "a.kicad_pcb"),
                                   _b(tmp_path, nudged, "b.kicad_pcb"))
    assert delta["moved"] == []


def test_rotation_and_side_flip_detected(tmp_path):
    flipped = BASE.replace('(property "Reference" "R1") (at 10 10 0) (layer "F.Cu")',
                           '(property "Reference" "R1") (at 10 10 90) (layer "B.Cu")')
    delta = board_diff.diff_boards(_b(tmp_path, BASE, "a.kicad_pcb"),
                                   _b(tmp_path, flipped, "b.kicad_pcb"))
    assert delta["moved"][0]["drot"] == 90.0
    assert delta["moved"][0]["flipped"] is True


def test_added_and_removed_components(tmp_path):
    without = BASE.replace(
        '  (footprint "lib:C_0402" (property "Reference" "C1") (at 20 20 0) (layer "F.Cu")\n'
        '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "/VCC")))\n', "")
    delta = board_diff.diff_boards(_b(tmp_path, without, "a.kicad_pcb"),
                                   _b(tmp_path, BASE, "b.kicad_pcb"))
    assert delta["added_refs"] == ["C1"] and delta["removed_refs"] == []
    # ...and the reverse direction is symmetric.
    back = board_diff.diff_boards(_b(tmp_path, BASE, "c.kicad_pcb"),
                                  _b(tmp_path, without, "d.kicad_pcb"))
    assert back["removed_refs"] == ["C1"] and back["added_refs"] == []


def test_changed_footprint_id(tmp_path):
    refp = BASE.replace('"lib:R_0402" (property "Reference" "R1")',
                        '"lib:R_0805" (property "Reference" "R1")')
    delta = board_diff.diff_boards(_b(tmp_path, BASE, "a.kicad_pcb"),
                                   _b(tmp_path, refp, "b.kicad_pcb"))
    assert delta["changed_footprints"] == [
        {"ref": "R1", "from": "lib:R_0402", "to": "lib:R_0805"}]


def test_net_membership_change(tmp_path):
    moved_pad = BASE.replace('(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "/VCC")))\n)',
                             '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 "")))\n)')
    delta = board_diff.diff_boards(_b(tmp_path, BASE, "a.kicad_pcb"),
                                   _b(tmp_path, moved_pad, "b.kicad_pcb"))
    assert "/VCC" in delta["changed_nets"]


def test_copper_deltas(tmp_path):
    extra = BASE.replace(
        '  (footprint "lib:R_0402"',
        '  (segment (start 5 5) (end 6 5) (width 0.2) (layer "F.Cu") (net "/VCC") (uuid "z"))\n'
        '  (footprint "lib:R_0402"')
    delta = board_diff.diff_boards(_b(tmp_path, BASE, "a.kicad_pcb"),
                                   _b(tmp_path, extra, "b.kicad_pcb"))
    assert delta["track_delta"] == 1


# -- the git read path -------------------------------------------------------

def _repo(tmp_path):
    """A throwaway git repo with two board revisions. Local only, no network."""
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    board = tmp_path / "board.kicad_pcb"
    board.write_text(BASE)
    git("add", "-A")
    git("commit", "-qm", "first")
    board.write_text(BASE.replace("(at 10 10 0)", "(at 15 10 0)"))
    git("add", "-A")
    git("commit", "-qm", "second")
    return tmp_path, board


def test_blob_at_reads_history_without_touching_the_working_tree(tmp_path):
    """The anti-clobber guarantee: `git show` must leave uncommitted work alone."""
    root, board = _repo(tmp_path)
    board.write_text(BASE.replace("(at 20 20 0)", "(at 99 99 0)"))   # uncommitted edit
    before = board.read_text()

    old = board_diff.blob_at(root, "HEAD~1", "board.kicad_pcb")
    assert old is not None and b"(at 10 10 0)" in old

    assert board.read_text() == before          # working tree untouched
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert "board.kicad_pcb" in status          # the edit is still there, still dirty


def test_board_at_parses_a_historic_revision(tmp_path):
    root, _ = _repo(tmp_path)
    old = board_diff.board_at(root, "HEAD~1", "board.kicad_pcb")
    new = board_diff.board_at(root, "HEAD", "board.kicad_pcb")
    delta = board_diff.diff_boards(old, new)
    assert delta["moved"] == [{"ref": "R1", "dx": 5.0, "dy": 0.0,
                               "drot": 0.0, "flipped": False}]


def test_missing_revision_returns_none(tmp_path):
    root, _ = _repo(tmp_path)
    assert board_diff.blob_at(root, "HEAD", "nope.kicad_pcb") is None
    assert board_diff.board_at(root, "no-such-rev", "board.kicad_pcb") is None


def test_revisions_lists_commits_touching_the_board(tmp_path):
    root, _ = _repo(tmp_path)
    revs = board_diff.revisions(root, "board.kicad_pcb")
    assert [r["subject"] for r in revs] == ["second", "first"]
    assert all(len(r["sha"]) == 40 for r in revs)


# -- overlays ----------------------------------------------------------------

SVG_A = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
         b'<path d="M0 0 L5 5" stroke="#C83434"/>'
         b'<rect fill="#000000" width="1" height="1"/></svg>')
SVG_B = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
         b'<path d="M0 0 L9 9" stroke="#C83434"/></svg>')


def test_overlay_svg_tints_each_revision_and_keeps_the_viewbox():
    from kicad_bench.core import imgdiff
    out = imgdiff.overlay_svg(SVG_A, SVG_B)
    assert b'viewBox="0 0 10 10"' in out          # geometry stays aligned
    assert out.count(b"mix-blend-mode:screen") == 2
    assert b"#FF5050" in out and b"#50C8FF" in out
    assert b"#C83434" not in out                  # every source colour was replaced
    assert b'fill="#000000"' in out               # ...except knockout black
    assert out.count(b"</svg>") == 1              # one well-formed document


def test_overlay_svg_rejects_a_non_svg():
    import pytest
    from kicad_bench.core import imgdiff
    with pytest.raises(RuntimeError):
        imgdiff.overlay_svg(b"not an svg", SVG_B)


def test_pixel_overlay_classifies_added_removed_and_shared():
    """The Pillow path, exercised on synthetic images so no renderer is involved."""
    import pytest
    Image = pytest.importorskip("PIL.Image")
    from kicad_bench.core import imgdiff
    import io

    def png(boxes):
        im = Image.new("L", (10, 10), 0)
        for x0, y0, x1, y1 in boxes:
            for x in range(x0, x1):
                for y in range(y0, y1):
                    im.putpixel((x, y), 255)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    old = png([(0, 0, 4, 4)])                     # 16 px
    new = png([(2, 2, 6, 6)])                     # 16 px, overlapping 2x2 = 4
    out, stats = imgdiff.overlay(old, new)
    assert stats["removed_px"] == 12 and stats["added_px"] == 12
    assert stats["total_px"] == 100
    assert stats["changed_pct"] == 24.0
    assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_pixel_overlay_refuses_mismatched_sizes():
    import pytest, io
    Image = pytest.importorskip("PIL.Image")
    from kicad_bench.core import imgdiff

    def png(size):
        buf = io.BytesIO()
        Image.new("L", size, 0).save(buf, format="PNG")
        return buf.getvalue()

    with pytest.raises(RuntimeError, match="differ in size"):
        imgdiff.overlay(png((10, 10)), png((12, 10)))
