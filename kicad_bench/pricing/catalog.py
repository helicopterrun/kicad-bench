"""catalog.py — vendor rate tables + user pricing prefs.

Defaults are the numbers calibrated in `docs/PRICING_MODEL.md` (as_of 2026-07-16), so
`kb price` works with zero config. A project overrides any of them via `[pricing]` /
`[tax]` in `kicad-bench.toml` (read from `cfg.raw`) — the same deep-merge idea the rest of
the config uses. Every rate carries an `as_of`; stale rates are the user's cue to refresh.

Nothing here is authoritative pricing — it's a rough, dated snapshot. Vendor quote engines win.
"""
from __future__ import annotations

from ..core import config as cfgmod

# -- calibrated defaults (see docs/PRICING_MODEL.md §11) ----------------------
DEFAULTS: dict = {
    "as_of": "2026-07-16",
    "oshpark": {
        "confidence": "exact",
        "source_url": "https://docs.oshpark.com/services/",
        "copies_multiple": 3,
        # usd per in², keyed by (layers, tier)
        "rate": {
            "2:proto": 5.0,
            "4:proto": 10.0,
            "4:swift": 20.0,
            "2:medium_run": 1.0,
            "4:medium_run": 2.0,
        },
        "medium_run_min_in2": 100,
        "shipping_flat_usd": 11.0,   # FedEx 2 Day, US — calibrated, not free
    },
    "jlcpcb": {
        "confidence": "estimate",
        "source_url": "https://jlcpcb.com/quote",
        "promo_floor_usd": 2.0,      # small boards land here (qty 5)
        # COARSE single-anchor bare-board rate (usd per in², per qty-5 set): LVDS 17.44 in²
        # 4-layer/ENIG bare ran $88.95/5 → ~$5.10/in². P2 replaces this with a real tier table.
        "coarse_rate_per_in2": {"1": 1.6, "2": 1.6, "4": 5.1, "6": 9.0},
        "landed": {
            "shipping_flat_usd": 45.0,   # express China→US; ~flat $40–50 observed
            "duty_pct": 33.0,            # ~33% of merchandise, 2026 tariff regime — VOLATILE
        },
        "assembly": {
            "setup_usd": 8.0,
            "per_joint_usd": 0.0017,
            "extended_part_loading_usd": 3.0,
        },
    },
    "pikkolo": {
        "confidence": "estimate",
        "source_url": "https://www.pikkoloassembly.com/",
        "setup_usd": 0.50,
        "per_placement_usd": 0.348,
        "component_markup_pct": 20.0,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Catalog:
    """Merged vendor rate tables + tax prefs for one project."""

    def __init__(self, data: dict, tax: dict, vendors: list[str], default_qty: int):
        self.data = data
        self.tax = tax
        self.vendors = vendors
        self.default_qty = default_qty

    def vendor(self, name: str) -> dict:
        return self.data.get(name, {})

    @classmethod
    def from_config(cls, cfg: cfgmod.Config | None) -> "Catalog":
        raw = getattr(cfg, "raw", {}) or {}
        pricing = raw.get("pricing", {}) if isinstance(raw, dict) else {}
        tax = raw.get("tax", {}) if isinstance(raw, dict) else {}
        # per-vendor overrides may live under [pricing.<vendor>]
        merged = _deep_merge(DEFAULTS, {k: v for k, v in pricing.items()
                                        if isinstance(v, dict)})
        vendors = pricing.get("vendors") or ["oshpark", "jlcpcb", "pikkolo"]
        default_qty = int(pricing.get("default_qty", 5))
        return cls(merged, tax, vendors, default_qty)
