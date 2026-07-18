"""partspec.py — best-effort electrical specs from BOM value strings.

Fallback spec source for the datasheet checks when no extracted `constraints.json`
exists for a part: parse what the schematic's Value field (plus free-text notes)
already says — "100nF 16V X7R", "4k7", "RED 2.0V 20mA". Anything not clearly stated
is returned as None; checks must *skip*, never guess, on missing fields.

Pure string parsing, no I/O.
"""
from __future__ import annotations

import re

_DIELECTRICS = {"C0G", "NP0", "X5R", "X6S", "X7R", "X7S", "X8R", "Y5V", "Z5U"}

_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
       "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}

# "100nF", "4.7uF", "0.1F"
_CAP = re.compile(r"^(\d+(?:\.\d+)?)([pnuµm]?)F$", re.IGNORECASE)
# "4u7" / "2n2" infix style capacitance
_CAP_INFIX = re.compile(r"^(\d+)([pnuµ])(\d+)$")
# "10k", "4.7M", "100R", "0R05", "10 ohm"
_RES = re.compile(r"^(\d+(?:\.\d+)?)([RkKMG]?)(?:Ω|OHMS?)?$")
_RES_INFIX = re.compile(r"^(\d+)([RkKMG])(\d+)$")
# "16V", "6.3V" (voltage rating for C, forward voltage for LED)
_VOLTS = re.compile(r"^(\d+(?:\.\d+)?)V$", re.IGNORECASE)
# "20mA", "1A", "150uA"
_AMPS = re.compile(r"^(\d+(?:\.\d+)?)([muµ]?)A$", re.IGNORECASE)


def _tokens(*texts: str) -> list[str]:
    toks: list[str] = []
    for t in texts:
        toks.extend(re.split(r"[\s,;/()]+", t.strip()))
    return [t for t in toks if t]


def parse_value(value: str, notes: str = "", hint: str = "") -> dict:
    """Parse a Value string (+ optional notes) into whatever specs it asserts.

    Returns a dict with any of: ``farads``, ``ohms``, ``v_rating`` (volts),
    ``amps``, ``dielectric``. Missing/unparseable fields are simply absent.

    `hint` disambiguates bare numbers: ``"R"`` makes a lone ``"100"`` parse as
    100 Ω (the KiCad convention for resistor values); otherwise bare numbers are
    ignored.
    """
    out: dict = {}
    for tok in _tokens(value, notes):
        m = _CAP.match(tok)
        if m and "farads" not in out:
            num, pre = m.groups()
            out["farads"] = float(num) * _SI[pre.lower() if pre != "M" else "m"]
            continue
        m = _CAP_INFIX.match(tok)
        if m and "farads" not in out:
            whole, pre, frac = m.groups()
            out["farads"] = float(f"{whole}.{frac}") * _SI[pre]
            continue
        m = _VOLTS.match(tok)
        if m and "v_rating" not in out:
            out["v_rating"] = float(m.group(1))
            continue
        m = _AMPS.match(tok)
        if m and "amps" not in out:
            num, pre = m.groups()
            out["amps"] = float(num) * _SI.get(pre.replace("µ", "u").lower(), 1.0)
            continue
        if tok.upper() in _DIELECTRICS and "dielectric" not in out:
            out["dielectric"] = "C0G" if tok.upper() == "NP0" else tok.upper()
            continue
        m = _RES_INFIX.match(tok)
        if m and "ohms" not in out:
            whole, pre, frac = m.groups()
            out["ohms"] = float(f"{whole}.{frac}") * _SI["" if pre == "R" else pre]
            continue
        m = _RES.match(tok)
        if m and "ohms" not in out:
            num, suffix = m.groups()
            # A bare number is a resistance only when the caller says so, or when an
            # explicit ohm marker / R-suffix was present.
            explicit = bool(suffix) or tok.upper().rstrip("S").endswith(("Ω", "OHM"))
            if explicit or hint.upper().startswith("R"):
                out["ohms"] = float(num) * _SI["" if suffix == "R" else suffix]
            continue
    return out


def dielectric_class(dielectric: str | None, value: str = "", notes: str = "") -> str:
    """Coarse capacitor class for derating policy: ceramic / tantalum / electrolytic.

    Ceramic is the default when nothing says otherwise (MLCCs dominate house designs);
    callers that need certainty should rely on extracted constraints instead.
    """
    text = " ".join(t for t in (dielectric or "", value, notes) if t).lower()
    if "tant" in text:
        return "tantalum"
    if "elec" in text or "aluminum" in text or "alu " in text:
        return "electrolytic"
    return "ceramic"
