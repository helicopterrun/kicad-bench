"""vref.py — regulator feedback reference voltages, and fixed-output detection.

To solve a rail from its feedback divider you need the regulator's internal reference:
`Vout = Vref · (1 + Rtop/Rbot)`. Two sources, in this order:

1. the extracted datasheet constraints (`spec.vref` in `constraints.json`) — page-cited,
   part-specific, and always preferred;
2. this table, keyed by MPN prefix — the fallback for parts whose datasheet hasn't been
   ingested, so a fresh board gets useful rails before anyone runs an extraction.

Just as important is knowing when *not* to do divider math at all. Most jellybean
regulators are fixed-output parts whose voltage is encoded in the MPN suffix
(`AP2112K-3.3`, `RT9013-28`, `AMS1117-3.3`, `LM7805`) and whose FB pin ties straight to
the output rail. `lookup()` reports that as `source="fixed_suffix"`, which tells the
caller to take the suffix voltage and skip the divider walk entirely.

Attribution: the family table and the suffix-parsing cascade are adapted from
`aklofas/kicad-happy` (MIT, Copyright (c) 2025 Andrew Klofas),
`skills/kicad/scripts/kicad_utils.py`. Modified: trimmed to the families we see, made
MPN-only (no lib_id), and returns a typed tuple. The underlying Vref values are
datasheet facts.

Read-only, stdlib-only.
"""
from __future__ import annotations

import re

# MPN prefix -> internal feedback reference, volts. Longest-prefix wins, so a specific
# entry beats a broad family one. Values are datasheet figures.
_VREF: dict[str, float] = {
    # Texas Instruments — switchers
    "TPS5430": 1.221, "TPS5450": 1.221, "TPS5410": 1.221,
    "TPS54160": 0.8, "TPS54260": 0.8, "TPS54360": 0.8,
    "TPS54040": 0.8, "TPS54060": 0.8,
    "TPS54302": 0.596, "TPS54308": 0.596,
    "TPS56339": 0.8, "TPS56637": 0.6, "TPS561": 0.6, "TPS562": 0.768,
    "TPS5632": 0.76, "TPS5633": 0.8, "TPS566": 0.6,
    "TPS560200": 0.8, "TPS6300": 0.5, "TPS6301": 0.5, "TPS6310": 0.5,
    "TPS6100": 0.5, "TPS6102": 0.595, "TPS6103": 0.595,
    "TPS6291": 0.8,
    "LMR51410": 0.6, "LMR51420": 0.6, "LMR51430": 0.6,
    "LMR51440": 0.8, "LMR51460": 0.8,
    "LMR51610": 0.6, "LMR51620": 0.6, "LMR51630": 0.6,
    "LMR336": 1.0, "LMR338": 1.0, "LMR380": 1.0,
    "LM258": 1.23, "LM259": 1.23, "LM614": 1.0, "LM619": 1.0, "LM2267": 1.285,
    # Texas Instruments — LDOs
    "TPS7A49": 1.194, "TPS7A45": 1.21, "TPS7A47": 1.4,
    "TPS7A25": 1.24, "TPS7A26": 1.24, "TPS7A92": 0.8,
    "TPS7A70": 0.5, "TPS7A73": 0.9, "TPS7A16": 1.2,
    "TPS7A30": 1.18, "TPS7A33": 1.18,           # negative rails: |Vref|
    "TPS736": 1.204, "TLV759": 0.55,
    # Analog Devices / Linear
    "LT361": 0.6, "LT362": 0.6, "LT810": 0.97, "LT811": 0.97,
    "LT860": 0.97, "LT862": 0.97, "LT871": 1.213, "LTM46": 0.6,
    # Diodes Inc
    "AP632": 0.8, "AP633": 0.8, "AP2112": 0.8, "AP3015": 1.23,
    "AP7335": 0.8, "AP7362": 0.6, "AP7363": 0.6, "AP7365": 0.8, "AP7366": 0.8,
    # Monolithic Power
    "MP1": 0.8, "MP2307": 0.925, "MP2315": 0.8, "MP2338": 0.5, "MP2359": 0.81,
    "MP2384": 0.6, "MP2403": 0.8, "MP2451": 0.8, "MP2459": 0.81, "MP2236": 0.6,
    "MP2143": 0.6, "MP2162": 0.6, "MP2303": 0.8, "MP28167": 1.0,
    # Richtek / Silergy / Microchip / ON / ST / Maxim / Renesas / XLSemi
    "RT5": 0.6, "RT6": 0.6, "RT2875": 0.6, "SY8": 0.6, "MIC29": 1.24,
    "LD1117": 1.25, "LDL1117": 1.25, "NCP1117": 1.25,
    "MAX5035": 1.22, "MAX5033": 1.22, "MAX1771": 1.5, "MAX1709": 1.25,
    "MAX17760": 0.8, "ISL854": 0.6, "ISL850": 0.8, "XL70": 1.25,
    # Generic adjustable classics
    "LM317": 1.25, "LM337": 1.25, "AMS1117": 1.25, "AMS1085": 1.25, "LM1117": 1.25,
}

# LM78xx/LM79xx encode the output voltage with no separator: LM7805 = 5 V.
_LM78 = re.compile(r"LM7[89][A-Z]?(\d{2})")
_SUFFIX_SHORTHAND = re.compile(r"[-_](\d+)V(\d+)")          # -3V3
_SUFFIX_DECIMAL = re.compile(r"[-_](\d+\.\d+)V?(?=[^0-9]|$)")  # -3.3
_SUFFIX_TWO_DIGIT = re.compile(r"[-_](\d{2})(?=[^0-9.]|$)")    # -33 -> 3.3, -12 -> 12
_INTEGER_RAILS = {10, 12, 15, 24, 48}
_LM78_RAILS = {5, 6, 8, 9, 10, 12, 15, 18, 24}


def lookup(mpn: str, value: str = "") -> tuple[float | None, str]:
    """(volts, source) for a regulator MPN.

    `source` is:
      * `"fixed_suffix"` — the MPN encodes a FIXED OUTPUT voltage. The number is Vout,
        not Vref, and the caller must **not** run divider math on this part.
      * `"family"` — the number is the part's internal Vref, from the table.
      * `""` — unknown; the caller skips.
    """
    for cand in (c.strip().upper() for c in (mpn, value) if c and c.strip()):
        m = _LM78.match(cand)
        if m and int(m.group(1)) in _LM78_RAILS:
            return float(m.group(1)), "fixed_suffix"

        m = _SUFFIX_SHORTHAND.search(cand)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}"), "fixed_suffix"
        m = _SUFFIX_DECIMAL.search(cand)
        if m and 0.5 <= float(m.group(1)) <= 60:
            return float(m.group(1)), "fixed_suffix"
        m = _SUFFIX_TWO_DIGIT.search(cand)
        if m:
            digits = m.group(1)
            if int(digits) in _INTEGER_RAILS:
                return float(digits), "fixed_suffix"
            v = float(f"{digits[0]}.{digits[1]}")
            if 0.5 <= v <= 9.9:
                return v, "fixed_suffix"

        best_prefix, best_vref = "", None
        for prefix, vref in _VREF.items():
            if cand.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix, best_vref = prefix, vref
        if best_vref is not None:
            return best_vref, "family"
    return None, ""
