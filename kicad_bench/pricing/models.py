"""models.py — per-vendor price models + the shared landed-cost scaffold.

Each model is a pure function of a `PriceSpec` + `Catalog` → a `Quote` (itemized, so the
output can explain the number). Confidence per §7 of the design doc:
  * OSH Park board — `exact` (closed-form area × rate).
  * JLCPCB — `estimate` (P1 uses a COARSE single-anchor bare-board rate; P2 swaps in a real
    tier table). Assembly is a follow-up (P3).
  * Pikkolo — `estimate`, assembly-only; components need BOM costing (P3).

`landed()` adds shipping + duty + sales-tax + discount lines to a merchandise subtotal. Those
sub-lines are always `estimate` (they drift with courier/tariff/state), so any landed total
rolls up to `estimate` even when the board line is exact.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .catalog import Catalog
from .spec import PriceSpec

# confidence ordering for roll-up (lowest wins)
_RANK = {"exact": 3, "estimate": 2, "manual": 1}


def _rollup(*confidences: str) -> str:
    got = [c for c in confidences if c]
    if not got:
        return "estimate"
    return min(got, key=lambda c: _RANK.get(c, 2))


@dataclass
class LineItem:
    label: str
    amount_usd: float
    confidence: str = "estimate"


@dataclass
class Quote:
    vendor: str
    service: str                      # "fab" | "assembly" | "fab+assembly"
    items: list[LineItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True                   # False when the vendor can't build this spec

    @property
    def total(self) -> float:
        return round(sum(i.amount_usd for i in self.items), 2)

    @property
    def confidence(self) -> str:
        return _rollup(*(i.confidence for i in self.items))


def landed(merchandise_usd: float, vendor_cfg: dict, tax: dict,
           discount_usd: float = 0.0) -> list[LineItem]:
    """Shipping + duty + sales-tax + discount lines for a merchandise subtotal."""
    out: list[LineItem] = []
    lc = vendor_cfg.get("landed", {})
    ship = lc.get("shipping_flat_usd", vendor_cfg.get("shipping_flat_usd"))
    if ship is not None:
        out.append(LineItem("Shipping (est.)", round(float(ship), 2), "estimate"))
    duty_pct = lc.get("duty_pct")
    if duty_pct:
        out.append(LineItem(f"Customs duty ~{duty_pct:g}% (est., volatile)",
                            round(merchandise_usd * float(duty_pct) / 100.0, 2), "estimate"))
    if discount_usd:
        out.append(LineItem("Discount / coupon", -abs(round(discount_usd, 2)), "estimate"))
    rate = tax.get("rate_pct")
    if rate:
        base = merchandise_usd + (float(ship) if ship else 0.0)
        out.append(LineItem(f"Sales tax {rate:g}% ({tax.get('state', '?')})",
                            round(base * float(rate) / 100.0, 2), "estimate"))
    return out


# -- OSH Park — exact --------------------------------------------------------
class OshParkModel:
    name = "oshpark"
    services = frozenset({"fab"})

    # OSH Park proto is 6/6 mil, 13 mil drill — a board finer than this is out of capability.
    _MIN_TRACE_MM = 0.152
    _MIN_DRILL_MM = 0.33

    def price(self, spec: PriceSpec, cat: Catalog, qty: int, tier: str = "proto") -> Quote:
        vc = cat.vendor("oshpark")
        conf = vc.get("confidence", "exact")
        q = Quote(vendor="OSH Park", service="fab")
        if spec.area_in2 is None:
            q.ok = False
            q.notes.append("area unknown — cannot price (need Edge.Cuts outline)")
            return q
        layers = spec.layers or 2
        key = f"{layers}:{tier}"
        rate = vc.get("rate", {}).get(key)
        if rate is None:
            q.ok = False
            q.notes.append(f"no OSH Park rate for {layers}-layer / {tier}")
            return q
        board = spec.area_in2 * rate
        mult = vc.get("copies_multiple", 3)
        if tier == "medium_run":
            min_in2 = vc.get("medium_run_min_in2", 100)
            billed_in2 = max(spec.area_in2 * qty, min_in2)
            board = billed_in2 * rate
            q.items.append(LineItem(f"{layers}L medium-run {billed_in2:.0f} in² × ${rate:g}",
                                    round(board, 2), conf))
        else:
            sets = math.ceil(qty / mult)
            copies = sets * mult
            board = spec.area_in2 * rate * sets
            q.items.append(LineItem(
                f"{layers}L {tier} {spec.area_in2:.2f} in² × ${rate:g} × {sets} set"
                f"{'s' if sets > 1 else ''} ({copies} copies)",
                round(board, 2), conf))
        ship = vc.get("shipping_flat_usd")
        if ship:
            q.items.append(LineItem("Shipping (FedEx 2 Day, est.)", round(float(ship), 2), "estimate"))
        # capability flags (do not block, just warn)
        fab_min = _fab_min(spec)
        if fab_min.get("trace") and fab_min["trace"] < self._MIN_TRACE_MM:
            q.notes.append(f"⚠ min trace {fab_min['trace']}mm < OSH Park 6 mil — over capability")
        if fab_min.get("drill") and fab_min["drill"] < self._MIN_DRILL_MM:
            q.notes.append(f"⚠ min drill {fab_min['drill']}mm < OSH Park 13 mil — over capability")
        return q


def _fab_min(spec: PriceSpec) -> dict:
    # PriceSpec doesn't carry fab_min today; capability check is best-effort/no-op if absent.
    return getattr(spec, "fab_min", {}) or {}


# -- JLCPCB — coarse estimate (P1); tier table is P2 -------------------------
class JlcpcbModel:
    name = "jlcpcb"
    services = frozenset({"fab"})

    def price(self, spec: PriceSpec, cat: Catalog, qty: int, tier: str = "proto") -> Quote:
        vc = cat.vendor("jlcpcb")
        conf = vc.get("confidence", "estimate")
        q = Quote(vendor="JLCPCB", service="fab")
        if spec.area_in2 is None:
            q.ok = False
            q.notes.append("area unknown — cannot price")
            return q
        layers = spec.layers or 2
        per_in2 = vc.get("coarse_rate_per_in2", {}).get(str(layers))
        if per_in2 is None:
            q.ok = False
            q.notes.append(f"no JLC coarse rate for {layers}-layer")
            return q
        floor = vc.get("promo_floor_usd", 2.0)
        # coarse_rate is calibrated to a qty-5 set; scale linearly off 5 as a rough proxy
        board = max(floor, spec.area_in2 * per_in2 * (qty / 5.0))
        q.items.append(LineItem(
            f"{layers}L bare ≈ {spec.area_in2:.2f} in² (coarse, qty {qty})",
            round(board, 2), conf))
        q.notes.append("COARSE single-anchor rate — P2 replaces with JLC tier table")
        # landed
        for li in landed(board, vc, cat.tax):
            q.items.append(li)
        q.notes.append("landed dominated by shipping+duty for small China→US orders")
        return q


# -- registry ----------------------------------------------------------------
FAB_MODELS = {"oshpark": OshParkModel(), "jlcpcb": JlcpcbModel()}


def fab_quotes(spec: PriceSpec, cat: Catalog, qty: int, tier: str = "proto") -> list[Quote]:
    out = []
    for name in cat.vendors:
        m = FAB_MODELS.get(name)
        if m:
            out.append(m.price(spec, cat, qty, tier))
    return out
