"""ipc.py — closed-form IPC transmission-line and current-capacity math.

The "grounded in the physics" sibling to the datasheet-grounded layer. `track-conformance`
checks a track against a *config floor* — a number someone typed. These functions check
against the published models instead: does this diff pair actually hit its target
impedance on the stackup the board itself declares?

Every function is pure `math` over scalars and cites the standard it implements. They are
deliberately reimplemented from the published equations rather than copied from any
project: the IPC formulas are facts, and reimplementing lets this module be **mm-native**
throughout, matching `pcbgeom` and the rest of kicad-bench. (The reference
implementations in the wild are mils-native; mixing the two is a permanent bug source.)

Accuracy: the IPC-2141 closed forms are curve fits, good to roughly ±10% against a field
solver, and they model a trace over a *solid* reference plane. A coplanar pour beside the
pair lowers the real impedance by a few percent, so a computed value is evidence for a
warning, never for an error.

Read-only, stdlib-only. Raises ValueError on non-physical geometry so callers skip.
"""
from __future__ import annotations

import math

# IPC-2221 current-capacity curve fit: I = k · ΔT^0.44 · A^0.725, A in mil².
_K_EXTERNAL = 0.048          # outer layers (free convection)
_K_INTERNAL = 0.024          # inner layers (conduction only) — half the external k
_DT_EXP = 0.44
_AREA_EXP = 0.725
_MM_PER_MIL = 0.0254
_OZ_TO_MM = 0.0348           # 1 oz/ft² copper ≈ 34.8 µm


def microstrip_z0(w_mm: float, h_mm: float, t_mm: float, er: float) -> float:
    """Surface-microstrip characteristic impedance, ohms. IPC-2141 §4.2.1.

        Z0 = 87/sqrt(er + 1.41) · ln(5.98h / (0.8w + t))

    `h_mm` is the dielectric height between the trace and its reference plane, `t_mm` the
    copper thickness. Valid roughly for 0.1 < w/h < 3.0 and 1 < er < 15.
    """
    _require_positive(w=w_mm, h=h_mm, er=er)
    if t_mm < 0:
        raise ValueError("copper thickness must be non-negative")
    denom = 0.8 * w_mm + t_mm
    if denom <= 0:
        raise ValueError("effective conductor width must be positive")
    return (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h_mm / denom)


def stripline_z0(w_mm: float, h_mm: float, t_mm: float, er: float) -> float:
    """Symmetric-stripline characteristic impedance, ohms. IPC-2141 §4.2.2.

        Z0 = 60/sqrt(er) · ln(4h / (0.67π(0.8w + t)))

    NOTE `h_mm` here is the **full plane-to-plane** dielectric thickness, not the height
    to one plane — the trace is centred between them. Getting this wrong is the classic
    stripline error.
    """
    _require_positive(w=w_mm, h=h_mm, er=er)
    if t_mm < 0:
        raise ValueError("copper thickness must be non-negative")
    denom = 0.67 * math.pi * (0.8 * w_mm + t_mm)
    if denom <= 0:
        raise ValueError("effective conductor width must be positive")
    return (60.0 / math.sqrt(er)) * math.log(4.0 * h_mm / denom)


def differential_z(z0: float, s_mm: float, h_mm: float,
                   geometry: str = "microstrip") -> float:
    """Edge-coupled differential impedance from the single-ended Z0. IPC-2141 §4.3.

        Zdiff = 2·Z0·(1 − 0.48·exp(−0.96·s/h))     (microstrip)
        Zdiff = 2·Z0·(1 − 0.347·exp(−2.9·s/h))     (stripline)

    `s_mm` is the edge-to-edge gap between the two traces. As the pair separates the
    coupling term decays and Zdiff approaches 2·Z0, which is the sanity check.
    """
    _require_positive(z0=z0, s=s_mm, h=h_mm)
    ratio = s_mm / h_mm
    if geometry == "microstrip":
        k = 1.0 - 0.48 * math.exp(-0.96 * ratio)
    elif geometry == "stripline":
        k = 1.0 - 0.347 * math.exp(-2.9 * ratio)
    else:
        raise ValueError("geometry must be 'microstrip' or 'stripline'")
    return 2.0 * z0 * k


def width_for_current(current_a: float, copper_oz: float = 1.0,
                      temp_rise_c: float = 10.0, internal: bool = False) -> float:
    """Minimum track width in mm to carry `current_a`. IPC-2221 §6.2, eq. 6-4.

    Inverts the curve fit I = k · ΔT^0.44 · A^0.725 for the cross-section, then divides
    by the copper thickness. The constants are mil-native, so the conversion happens
    here rather than leaking into callers.

    This is the *bare IPC minimum* with no margin — real designs derate it. IPC-2152
    supersedes this model with better data; 2221 remains the widely-quoted baseline.
    """
    _require_positive(current=current_a, copper_oz=copper_oz, temp_rise=temp_rise_c)
    k = _K_INTERNAL if internal else _K_EXTERNAL
    area_mil2 = (current_a / (k * temp_rise_c ** _DT_EXP)) ** (1.0 / _AREA_EXP)
    thickness_mil = copper_oz * _OZ_TO_MM / _MM_PER_MIL
    return (area_mil2 / thickness_mil) * _MM_PER_MIL


def _require_positive(**vals) -> None:
    for name, v in vals.items():
        if v is None or v <= 0:
            raise ValueError(f"{name} must be positive (got {v!r})")
