# Peer-repo mining notes

Companion to `opensource-concept-mining.md`. That doc says *what* to mine; this one records
*where it actually is* after reading both peer repos in full, and whether it can be **lifted**
(copied, with attribution) or is **concept-only** (reimplement clean-room).

Clones live at `/root/refs/` — outside this repo, so they can never be staged by accident and
never pollute a `grep`/`rg`/pytest sweep here.

| Repo | Path | Licence | Size |
|------|------|---------|------|
| aklofas/kicad-happy | `/root/refs/kicad-happy` @ `f765dc0` v2.1.0 | MIT | ~80k LOC, 12 skills |
| salitronic/eda-agent | `/root/refs/eda-agent` | Apache-2.0 | ~66k LOC, 320 files |

Both licences confirmed via `gh api` (the research doc marked several "(check)"). Also
confirmed permissive and available if T1.4 needs them: `leoheck/kiri` MIT, `Gasman2014/KiCad-Diff` MIT.

---

## Attribution — what each licence actually requires

**kicad-happy (MIT).** `LICENSE`: `Copyright (c) 2025 Andrew Klofas`. Retain that line and the
MIT text with any copied code. That is the whole obligation.

**eda-agent (Apache-2.0).** Three obligations, one of which is easy to miss:

- §4(a) ship a copy of `LICENSE`.
- §4(c) keep the two-line SPDX header that every source file carries:
  ```python
  # SPDX-License-Identifier: Apache-2.0
  # Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
  ```
- §4(d) **a `NOTICE` file exists**, so its text must be propagated into our own NOTICE or an
  equivalent readable location:
  ```
  eda-agent
  Copyright 2026 George Saliba <george.saliba@salitronic.com>

  This product includes software developed by George Saliba
  (https://github.com/salitronic/eda-agent).
  ```
- §4(b) mark modified files as changed — append `# Modified from salitronic/eda-agent`.

No CLA trap, no GPL contamination in either.

**Recommendation: lift nothing for the IPC math.** The IPC-2141/2221 equations are published
standard formulas — facts, not creative expression. Reimplement from the section numbers and
cite *the standard*. That removes the attribution burden on the one item where lifting is most
tempting, and lets us pick our own unit convention (see the mils trap below).

---

## Per-step findings

### Step 3 — `vref` + feedback dividers (T1.1b)

**kicad-happy is the jackpot here, and it is directly liftable** (MIT, `re`-only, no coupling).

- `skills/kicad/scripts/kicad_utils.py:26-138` — `_REGULATOR_VREF`, **114 prefix entries
  covering ~65 regulator families**, each with an inline datasheet-verified comment. Includes
  `"AP632": 0.8` (covers the AP63200/03/05 family on our own board) and `"AP2112": 0.8`.
  Longest-prefix-match, so specific entries beat broad ones.
- `skills/kicad/scripts/kicad_utils.py:168-220` — `lookup_regulator_vref(value, lib_id)`
  returning **`(vref, source)` where `source ∈ {"fixed_suffix", "lookup", ""}`**. This is the
  single best idea in either repo for our step 3: `fixed_suffix` means *"the MPN encodes a
  fixed output voltage, so do NOT run divider math"* and short-circuits the whole FB path.
  Its regex cascade parses `AMS1117-3.3`, `RT9013-18`, `LM7805`, `TLV1117LV-33`, `-3V3`.
  **This directly solves the U9 (RT9013-28 → 2.8 V) gap** we hit in step 2, with no
  `constraints.json` entry needed at all.
- `skills/spice/scripts/spice_part_library.py:113-219` — a *second* table, `LDO_SPECS` +
  `VREF_SPECS` (TL431 2.5, TLV431 1.24, LM4040, REF30xx, MCP1541 4.096). Merge with the above;
  they mostly agree.
- Divider walk: `skills/kicad/scripts/signal_detectors.py:222-394` `detect_voltage_dividers`.
  Pairwise shared-net scan, not a graph walk. Its **rejection rules are the valuable part** and
  match what our plan already calls for, plus two we hadn't thought of:
  - exactly one shared net between the resistor pair, else skip (primary ambiguity guard);
  - orientation normalisation — swap if `top_net` is ground;
  - **reject bus mid-points**: if `mid_net` is power/ground and has >4 pins, skip;
  - **reject pull-up/pull-down pairs**: `max(r)/min(r) > 1000` → skip;
  - `_merge_series_dividers` extends through pass-through nodes, handling an Rtop split across
    two resistors.
- Regulator/FB-pin identification: `signal_detectors.py:1558-1740`. Pin-name driven with a
  composite-name split (`FB_1` → `FB`, `PG/FB` → `{PG, FB}`) and five hardcoded exclusion lists
  (EEPROM/UART/ESP32, LNA/MMIC, load switches, opamp/ADC) that are clearly corpus-tuned. Worth
  copying the exclusion lists verbatim.

**Do not copy its inverting-rail math.** `signal_detectors.py:1812-1827` computes the magnitude
with the normal divider formula and negates as a post-step, keyed off name heuristics
(`"invert"`, `"neg"`, a `-` in the net name). That is wrong for a true inverting FB network
where the divider straddles a negative output and a positive Vref — exactly the TPS65150 FBN
case on our board. Take the detection keywords; our plan's "skip inverting topologies" is the
correct call. Its heuristic Vref fallback (sweep `[0.6, 0.8, 1.0, 1.22, 1.25]`, first hit in
range wins) is degenerate — it almost always picks 0.6. Do not copy that either; unknown Vref
must stay `None`.

Free bonus: `skills/kicad/scripts/what_if.py:854-892` cross-checks a computed `estimated_vout`
against the voltage parsed from the rail's *name* and flags disagreement. That is a genuinely
new gate idea for us — we now have both numbers and provenance for each.

### Step 4 — ECO drift (T1.2)

**Concept-only.** eda-agent's real `obj_crossref_net` is DelphiScript
(`scripts/altium/Generic.pas:6522-6746`), not Python. Both sides reduce to a flat set of
`"Designator.PinNumber"` strings — which is exactly the shape our plan already chose.

Two design decisions worth stealing:

- **The `in_sync` verdict is symmetric-empty AND non-vacuous** (`Generic.pas:6731`):
  `in_sync = (sch_only == 0) and (pcb_only == 0) and (sch_count > 0 or pcb_count > 0)`.
  A net present on *neither* side reports `in_sync=false`, not `true` — so a typo'd net name
  can't read as "in sync". Replicate this.
- A `_diag` block of counters (`board_nil`, `iter_visited`, `pad_net_nil`) that distinguishes
  "board not found" from "iterator empty" from "no name match". Our `Result.info` skip line
  should carry the same disambiguation.

Its Python analogue `core/kicad_netlist.py:89-136 compare_schematic_to_pcb` is **coarser and
carries a latent bug**: it does raw set-difference on net *names* with zero normalisation, so
it reports false divergence on any hierarchical design. Do not inherit that — our measured
`lstrip("/")` normalisation (191/194 exact, 100% after pseudo-net filtering) is already better.
Its `parse_kicadxml_netlist` (~25 lines, stdlib) is a clean reference for pin-level membership.

### Step 5 — IPC physics (T1.3)

Both repos carry usable math; **eda-agent's is far better factored** (frozen dataclass returns,
`ValueError` on bad input, forward↔inverse round-trip tests).

- `design/trace_sizing.py` — IPC-2221 `I = k·ΔT^0.44·(h·w)^0.725`, `k=0.048` external /
  `0.024` internal, plus the exact algebraic inverse and `trace_resistance_mohm`.
- `design/impedance_sizing.py` — IPC-2141 microstrip and symmetric stripline Z₀, the
  differential coupling factor `k` in `Zdiff = 2·Z₀·k`, and the **exact algebraic inversion**
  (width for a target impedance — no solver needed). Returns a `feasible` flag rather than
  raising, which maps straight onto our `Result`.
- `design/signal_integrity.py` — termination (series/parallel/Thevenin/AC), Hammerstad
  effective-Er, critical length.
- `design/length_matching.py` — rests on the identity **ns/inch ≡ ps/mil**.
- `design/thermal_vias.py` — `R = L/(k·A)`, `K_COPPER = 385`.
- kicad-happy has a Wheeler-pair microstrip Z₀ at `analyze_pcb.py:2135-2175` (with a
  copper-thickness effective-width correction) and an IPC-2152 amps→width lookup table at
  `cross_analysis.py:95-111`. It has **no stripline and no differential** calculator.

**Three traps to avoid when we write `core/ipc.py`:**

1. **eda-agent's whole calculator family is mils-native** — widths, heights, thicknesses all in
   mils, `copper_oz` in oz/ft². Only `thermal_vias.py` is metric. `kicad_bench` is mm
   throughout (`pcbgeom` parses mm, the stackup is mm). Write ours **mm-native** and convert at
   the edge, or this becomes a permanent unit-bug generator.
2. **eda-agent contradicts itself on effective-Er**: `tools/pcb.py:2292` uses `er_eff=(er+1)/2`
   while `signal_integrity.py` uses Hammerstad. For er=4.2 that's 2.60 vs 2.67. Pick Hammerstad
   and use it everywhere.
3. Its forward single-ended/differential math is **inline in an MCP tool body**
   (`tools/pcb.py:2292-2350`), not in the `design/` module — easy to miss and grab the inverse
   only.

The differential formula we need, cross-checked and identical in both places:
`Zdiff = 2·Z₀·(1 − 0.48·exp(−0.96·s/h))` for edge-coupled microstrip. This is the form the plan
already validated against LVDS Driver V1's committed fab targets.

### Step 6 — git-diff surface (T1.4)

Neither peer covers it; `kiri` (MIT) and `KiCad-Diff` (MIT) are the sources, both shell
orchestrators around `kicad-cli`/`plotgitsch`. Nothing to lift beyond the pipeline shape.

### Not on the current plan, but worth a later look

- **Convention-inference footprint lint** (T2.5) — `design/footprint_policy.py`, 35 KB of pure
  `Counter`-based majority-vote inference, explicitly written Altium-free and unit-testable.
  Directly liftable. Its **MISPLACED vs STRAY distinction** (`_check_layer_role:211`) is the
  standout idea: graphics *only* on the wrong layer is a safe auto-fix; graphics on both the
  convention layer *and* elsewhere would stack duplicate geometry, so it stays manual. That is
  exactly the guardrail a read-first workbench wants.
- **Datasheet confidence scoring** (T3.1) — `skills/datasheets/scripts/datasheet_score.py`,
  five weighted dimensions, deterministic, zero imports. And
  `datasheet_types/trust_gating.py` (~125 lines) whose `min_confidence` is **keyword-only and
  required**, forcing every consumer to declare its trust level. Both liftable as-is.
- **Motif detection** (T2.1) — `design/motifs.py` **depends on networkx**, so the matcher is
  concept-only for us. But two ideas transfer: it uses **monomorphism, not induced
  isomorphism** (the host may have extra edges the pattern doesn't constrain — induced matching
  would reject most real circuits), and its false-positive control is a single
  `internal_nets` frozenset per motif requiring exact-degree match on closed nodes (that is
  what stops every R-R pair matching `voltage_divider`). The 15-motif catalogue is a compact
  declarative DSL and is portable even though the matcher isn't.
- **EMC formulas** (T2.3) — `skills/emc/scripts/emc_formulas.py` is `math`-only, every function
  carrying an inline `# Source:` citation (Ott / Bogatin / Johnson). Liftable. The 44-check
  rule engine on top of it is concept-only — it's bound to kicad-happy's own pre-digested
  `pcb.json`. The real asset is `skills/emc/references/pcb-emc-rules.md`, 380 lines of
  threshold + rationale + authoritative source.

---

## Summary

| Step | Verdict | Where |
|------|---------|-------|
| 3 — vref/dividers | **Lift** (MIT) | kicad-happy `kicad_utils.py:26-220`, `signal_detectors.py:222-394` |
| 4 — ECO drift | Concept-only | eda-agent `Generic.pas:6689-6733` (verdict rule + diag counters) |
| 5 — IPC math | **Reimplement** from the standards; use eda-agent `design/*.py` as the cross-check | mm-native, Hammerstad Er_eff |
| 6 — git diff | Concept-only | kiri / KiCad-Diff pipeline shape |
