"""scaffold tests — pure tree/file creation, no git, no kicad-cli."""
import pytest

from kicad_bench import scaffold
from kicad_bench.core import config as cfgmod


def test_valid_name():
    assert scaffold.valid_name("wildlife-cam")
    assert scaffold.valid_name("board1")
    assert not scaffold.valid_name("Wildlife_Cam")   # uppercase + underscore
    assert not scaffold.valid_name("-lead")          # leading dash
    assert not scaffold.valid_name("")


def test_scaffold_tree_and_seed_files(tmp_path):
    root = scaffold.scaffold_product(tmp_path, "wildlife-cam", ["main-board"])
    assert root == tmp_path / "wildlife-cam"
    # top-level tree
    for d in ("hardware/main-board/lib", "hardware/main-board/output",
              "firmware", "mechanical", "releases", "tools", "docs/datasheets"):
        assert (root / d).is_dir()
    # seed files (incl. the emitted CI workflow)
    for f in ("README.md", "CHANGELOG.md", ".gitignore", "kicad-bench.toml",
              "approved_parts.csv", "docs/architecture.md", "docs/requirements.md",
              "hardware/main-board/README.md", ".github/workflows/hardware-ci.yml"):
        assert (root / f).is_file()
    # CI drives the kb gates
    assert "kb approved-parts" in (root / ".github/workflows/hardware-ci.yml").read_text()
    # compliance-aware requirements stub carried over from the old workflow
    assert "FCC Part 15" in (root / "docs/requirements.md").read_text()
    # per-board fab-notes template
    assert "Stackup" in (root / "hardware/main-board/README.md").read_text()


def test_scaffold_writes_no_design_files(tmp_path):
    root = scaffold.scaffold_product(tmp_path, "prod", ["b1"])
    for pat in ("*.kicad_pro", "*.kicad_sch", "*.kicad_pcb"):
        assert list(root.rglob(pat)) == []          # read-first: no design files created


def test_scaffold_refuses_existing(tmp_path):
    scaffold.scaffold_product(tmp_path, "prod", ["b1"])
    with pytest.raises(FileExistsError):
        scaffold.scaffold_product(tmp_path, "prod", ["b1"])


def test_generated_config_loads_multi_board(tmp_path):
    """The wired-in kicad-bench.toml must load through the Phase-1 config and expose
    the boards — this is the scaffold ⋈ config integration check."""
    root = scaffold.scaffold_product(tmp_path, "climate-chamber",
                                     ["main-board", "sensor-board"])
    cfg = cfgmod.load(root / "kicad-bench.toml")
    assert cfg.product is not None and cfg.product.name == "climate-chamber"
    assert [b.name for b in cfg.boards] == ["main-board", "sensor-board"]
    # active board mirrors onto the top-level fields, paths resolved under the product
    assert cfg.root_sch == root / "hardware/main-board/main-board.kicad_sch"
    # approved-parts wiring present and its CSV exists on disk
    assert cfg.approved_parts is not None
    assert cfg.approved_parts.csv == root / "approved_parts.csv"
    assert cfg.approved_parts.csv.is_file()
