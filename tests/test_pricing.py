"""Pricing model tests — calibrated against real orders (docs/PRICING_MODEL.md §11)."""
import textwrap

from kicad_bench.pricing import geom, models
from kicad_bench.pricing.catalog import Catalog
from kicad_bench.pricing.spec import PriceSpec


# -- geometry: Edge.Cuts bbox ------------------------------------------------
_RECT_PCB = """
(kicad_pcb
  (gr_line (start 50 100) (end 50 175) (stroke (width 0.05)) (layer "Edge.Cuts"))
  (gr_line (start 50 175) (end 200 175) (stroke (width 0.05)) (layer "Edge.Cuts"))
  (gr_line (start 200 175) (end 200 100) (stroke (width 0.05)) (layer "Edge.Cuts"))
  (gr_line (start 200 100) (end 50 100) (stroke (width 0.05)) (layer "Edge.Cuts"))
  (gr_line (start 0 0) (end 999 999) (stroke (width 0.1)) (layer "F.SilkS"))
)
"""


def test_bbox_rectangle(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_RECT_PCB)
    bb = geom.board_outline_bbox(p)
    assert bb is not None
    assert bb.w_mm == 150 and bb.h_mm == 75          # ignores the silk line
    assert round(bb.area_in2, 2) == 17.44            # matches the real LVDS board


def test_bbox_none_when_no_outline(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb (gr_line (start 0 0) (end 1 1) (layer "F.SilkS")))')
    assert geom.board_outline_bbox(p) is None


# -- OSH Park: exact formula -------------------------------------------------
def _cat():
    return Catalog.from_config(None)   # calibrated DEFAULTS, no project


def _spec(area, layers, **kw):
    return PriceSpec(board_name="t", area_in2=area, width_mm=None, height_mm=None,
                     layers=layers, thickness_mm=None, copper_weight=None, finish=None, **kw)


def test_oshpark_documented_example():
    # OSH Park's own example: a 2 in² 4-layer board = $20 for 3 copies.
    q = models.OshParkModel().price(_spec(2.0, 4), _cat(), qty=3)
    board = next(i for i in q.items if "4L" in i.label)
    assert round(board.amount_usd, 2) == 20.00
    assert q.confidence == "estimate"                # rolls up to estimate via the shipping line
    assert board.confidence == "exact"


def test_oshpark_interposer_set_cost():
    # Real order: interposer bare = $11.60 / set of 3 (2-layer). Implies ~2.32 in².
    q = models.OshParkModel().price(_spec(2.32, 2), _cat(), qty=3)
    board = next(i for i in q.items if "2L" in i.label)
    assert round(board.amount_usd, 2) == 11.60
    ship = next(i for i in q.items if "Shipping" in i.label)
    assert ship.amount_usd == 11.00                  # FedEx 2 Day, calibrated (not free)


def test_oshpark_copies_round_up_to_multiple_of_three():
    # qty 4 → 2 sets of 3 (6 copies), so board cost doubles vs one set.
    one = models.OshParkModel().price(_spec(2.0, 2), _cat(), qty=3).items[0].amount_usd
    two = models.OshParkModel().price(_spec(2.0, 2), _cat(), qty=4).items[0].amount_usd
    assert round(two, 2) == round(2 * one, 2)


def test_oshpark_unknown_area_not_ok():
    q = models.OshParkModel().price(_spec(None, 2), _cat(), qty=3)
    assert not q.ok


# -- landed cost scaffold ----------------------------------------------------
def test_landed_lines_shipping_duty_tax():
    vc = _cat().vendor("jlcpcb")
    tax = {"rate_pct": 8.6, "state": "CO"}
    lines = models.landed(100.0, vc, tax, discount_usd=20.0)
    kinds = {l.label.split()[0]: l.amount_usd for l in lines}
    assert kinds["Shipping"] == 45.0
    assert round(kinds["Customs"], 2) == 33.0        # 33% of $100 merchandise
    assert kinds["Discount"] == -20.0
    # sales tax on (merch + shipping) = 8.6% of 145
    assert round(next(l.amount_usd for l in lines if l.label.startswith("Sales")), 2) == 12.47
    assert all(l.confidence == "estimate" for l in lines)


def test_jlc_small_board_hits_promo_floor():
    # A tiny board should floor at the $2 promo, not go below.
    q = models.JlcpcbModel().price(_spec(0.2, 2), _cat(), qty=5)
    board = q.items[0]
    assert board.amount_usd == 2.0
    assert q.confidence == "estimate"


# -- assembly: Pikkolo reproduces the real interposer invoice exactly --------
def test_pikkolo_interposer_invoice_exact():
    # Real Pikkolo invoice: 6-placement interposer, 1 board, bare parts $23.93.
    #   setup $0.50 + 6×$0.348 + $23.93×1.20 = $31.31
    spec = _spec(2.32, 2, n_placements=6)
    q = models.PikkoloModel().price(spec, _cat(), qty=1, bom_cost=23.93)
    assert round(q.total, 2) == 31.31
    assert not q.partial
    labels = {i.label.split()[0] for i in q.items}
    assert {"Setup", "Part", "Components"} <= labels


def test_pikkolo_partial_without_bom_cost():
    spec = _spec(2.32, 2, n_placements=6)
    q = models.PikkoloModel().price(spec, _cat(), qty=1)   # no bom_cost
    assert q.partial
    # labor still present (setup + placement)
    assert round(q.total, 2) == round(0.50 + 6 * 0.348, 2)


def test_pikkolo_placements_scale_with_qty():
    spec = _spec(2.32, 2, n_placements=6)
    q = models.PikkoloModel().price(spec, _cat(), qty=3, bom_cost=10.0)
    place = next(i for i in q.items if i.label.startswith("Part"))
    assert round(place.amount_usd, 2) == round(6 * 3 * 0.348, 2)
    comp = next(i for i in q.items if i.label.startswith("Components"))
    assert round(comp.amount_usd, 2) == round(10.0 * 3 * 1.20, 2)


# -- populated-board combo ---------------------------------------------------
def test_combo_oshpark_plus_pikkolo():
    spec = _spec(2.32, 2, n_placements=6)
    fabs = models.fab_quotes(spec, _cat(), qty=3)          # includes OSH Park
    asms = models.assembly_quotes(spec, _cat(), qty=3, bom_cost=10.0)
    combos = models.combo_rows(fabs, asms)
    combo = next(c for c in combos if "Pikkolo" in c.label)
    osh = next(q for q in fabs if q.vendor == "OSH Park").total
    pik = next(q for q in asms if q.vendor == "Pikkolo").total
    assert round(combo.total, 2) == round(osh + pik, 2)
    assert not combo.partial
