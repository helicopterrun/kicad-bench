"""template tests — hermetic: build a minimal fake template, instantiate, assert patches.

No dependency on the real kicad-templates repo or kicad-cli (validate=False).
"""
import json

import pytest

from kicad_bench import template as tmpl


def _fake_template(root, name="2L_OSHpark_Template"):
    """Create a minimal but structurally-real KiCad template dir under `root`."""
    d = root / name
    (d / "lib" / "fiducials.pretty").mkdir(parents=True)
    (d / "lib" / "mounting-holes" / "holes.pretty").mkdir(parents=True)
    pro = {
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "schematic": {"top_level_sheets": [
            {"filename": f"{name}.kicad_sch", "name": name, "uuid": "u-1"}]},
        "sheets": [["u-1", "4L_OSHpark_Template"]],   # stale copy-paste artifact
        "net_settings": {"classes": [{"name": "Default"}]},
    }
    (d / f"{name}.kicad_pro").write_text(json.dumps(pro, indent=2))
    (d / f"{name}.kicad_sch").write_text(
        f'(kicad_sch (generator_version "10.0")\n'
        f'  (symbol (instances (project "{name}" (path "/u-1"))))\n'
        f'  (symbol (instances (project "{name}" (path "/u-2"))))\n)\n')
    (d / f"{name}.kicad_pcb").write_text(
        f'(kicad_pcb (generator_version "10.0")\n'
        f'  (footprint (sheetfile "{name}.kicad_sch"))\n'
        f'  (property (type default))\n)\n')
    return d


def test_list_and_resolve(tmp_path):
    _fake_template(tmp_path)
    assert tmpl.list_templates(tmp_path) == ["2L_OSHpark_Template"]
    assert tmpl.resolve_template("2L_OSHpark_Template", tmp_path).name == "2L_OSHpark_Template"
    with pytest.raises(ValueError):
        tmpl.resolve_template("nope", tmp_path)


def test_instantiate_renames_and_patches(tmp_path):
    td = _fake_template(tmp_path)
    dest = tmp_path / "hardware" / "main-board"
    r = tmpl.instantiate_template(td, dest, "main-board", validate=False)

    assert r["files"] == ["main-board.kicad_pro", "main-board.kicad_sch", "main-board.kicad_pcb"]
    # design files renamed to the board basename
    for ext in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        assert (dest / f"main-board{ext}").is_file()

    # .kicad_pro name refs patched structurally (incl. the stale sheets artifact)
    pro = json.loads((dest / "main-board.kicad_pro").read_text())
    assert pro["meta"]["filename"] == "main-board.kicad_pro"
    assert pro["schematic"]["top_level_sheets"][0] == {
        "filename": "main-board.kicad_sch", "name": "main-board", "uuid": "u-1"}
    assert pro["sheets"] == [["u-1", "main-board"]]

    sch = (dest / "main-board.kicad_sch").read_text()
    pcb = (dest / "main-board.kicad_pcb").read_text()
    assert sch.count('(project "main-board"') == 2
    assert '(sheetfile "main-board.kicad_sch")' in pcb
    # the anchored patch must NOT touch the `(type default)` token
    assert "(type default)" in pcb

    # no stale template names anywhere
    for f in ("main-board.kicad_pro", "main-board.kicad_sch", "main-board.kicad_pcb"):
        text = (dest / f).read_text()
        assert "2L_OSHpark_Template" not in text
        assert "4L_OSHpark_Template" not in text


def test_fp_lib_table_generated(tmp_path):
    td = _fake_template(tmp_path)
    dest = tmp_path / "b"
    r = tmpl.instantiate_template(td, dest, "b", validate=False)
    assert set(r["footprint_libs"]) == {"fiducials", "holes"}
    table = (dest / "fp-lib-table").read_text()
    assert "(version 7)" in table
    assert '${KIPRJMOD}/lib/fiducials.pretty' in table
    assert '${KIPRJMOD}/lib/mounting-holes/holes.pretty' in table


def test_refuses_existing_design_file(tmp_path):
    td = _fake_template(tmp_path)
    dest = tmp_path / "b"
    tmpl.instantiate_template(td, dest, "b", validate=False)
    with pytest.raises(FileExistsError):
        tmpl.instantiate_template(td, dest, "b", validate=False)
