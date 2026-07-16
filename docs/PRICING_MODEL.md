# PCB Pricing Model — design spec

**Status:** proposal, awaiting sign-off. No code yet.
**Goal:** give a rough, apples-to-apples cost estimate for a board across multiple
fabricators/assemblers, and make *"which vendor + which stackup"* a one-line switch at
project kickoff — driven by the same board data `kicad-bench` already extracts.

Numbers in this doc were fetched live (see [Provenance](#7-provenance--confidence),
`as_of 2026-07-16`). They are pinned in editable config, never hard-coded, so a price
change is a one-line edit and every figure carries a `source_url` + date.

---

## 1. Scope & non-goals

**In scope**
- Bare-board **fabrication** cost estimate.
- **Assembly** (PCBA) cost estimate, as a *separate* line — because the three vendors the
  user actually uses are not the same kind of service (see §2).
- A per-vendor **comparison table** at a chosen quantity, plus a per-vendor cost breakdown.
- Re-costing on a **stackup/layer/finish** change (2-layer vs 4-layer respin, 1oz vs 2oz, …).

**Explicitly out of scope (v1)** — these are the fuzzy parts; we label them rather than fake them:
- **Shipping** — hugely variable (courier, destination, weight, consolidation). Shown as a
  separate, clearly-estimated or excluded line, never folded into the board price.
- **Tariffs / duties** — destination-dependent; out.
- **Promos / coupons** — JLC's "$2 / free-assembly-coupon" churn constantly. We cost the
  *list* price and note where a standing promo usually applies.
- **Exact-quote parity** — this is a *rough* estimate to compare options, not a substitute
  for the vendor's own quote engine at checkout.

---

## 2. Key insight — three vendors, three service shapes

Research turned up something that shapes the whole model: **the three vendors are not
interchangeable.**

| Vendor | Bare-board fab? | Assembly? | Character |
|---|:--:|:--:|---|
| **OSH Park** | ✅ | ❌ | US proto/medium-run bare boards. **Pure `area × rate`** — the easiest, most predictable model that exists. Ground-truth anchor. |
| **JLCPCB** | ✅ | ✅ | Tiered, promo-driven, does everything. The complex model. |
| **Pikkolo** | ❌ | ✅ | Erie, CO assembly house, **assembly only** (~$0.35/placement, $0.50 setup, turnkey w/ components at cost +20%). Pairs with someone else's bare board. |

**Consequence:** the model separates **fab** cost from **assembly** cost, and a *populated-
board* quote can combine two vendors (e.g. **OSH Park board + Pikkolo assembly**, or JLC for
both). The comparison output should therefore be able to show mix-and-match rows, not just
one vendor per line.

**This is confirmed by a real order** (see [§11 Calibration](#11-calibration--real-orders)):
the user's `interposer-v1-pikkolo` was **fabbed by OSH Park and shipped direct to Pikkolo's
address** for assembly. The mix-vendor combo isn't a nice-to-have — it's the primary
workflow, so the "populated-board combo" row is a first-class output, not an extra.

---

## 3. Input taxonomy — what's free vs. what needs a model

The board *spec* is almost entirely derivable from files `kicad-bench` already parses. The
only genuinely new thing is the **vendor pricing catalog**.

### 3a. Free from the board (extract, don't ask)
| Input | Source in repo today |
|---|---|
| Board area (in² / cm²) + outline W×H | `core/pcbgeom.py` → Edge.Cuts bounding box (add a `board_bbox()` helper) |
| Layer count | `fab.config` JSON (`layers`) / `.kicad_pro` stackup |
| Board thickness | `fab.config` (`board_thickness`) |
| Copper weight | `fab.config` (`copper_weight`) |
| Min trace/space/drill/via → **tech tier** | `fab.config` (`fab_min`) — decides "standard" vs "advanced" adders |
| Surface finish, mask color | stackup / `fab.config` (add fields if absent) |
| Unique-part count + total placements/joints → **assembly** | `bom_assembly.py` + BOM (`Driver_BOM_JLCPCB.xlsx`) |
| SMD vs TH split, sides populated | BOM + footprint pad types (`core/footprints.py`) |

### 3b. New — the vendor pricing catalog (config)
Rates, area/qty tiers, per-option adders, assembly setup + per-joint + per-unique-part,
minimums. One editable file per install; see §6.

### 3c. Assumed / defaulted (overridable)
Quantity, panelization (v-cut/tab), lead-time tier (proto vs medium-run vs swift),
destination region (for the shipping *note* only).

---

## 4. Architecture

Mirror the seam already in `fab.py` (the `FabBackend` Protocol). One `PriceModel` per
vendor; each is a **pure function** of a normalized spec → a structured breakdown. No model
reads a design file — it consumes a `PriceSpec` the extractor produced.

```
                 ┌──────────────────────────────────────────┐
   .kicad_pcb ──▶│ spec extractor (reuses pcbgeom /          │──▶ PriceSpec
   fab.config ──▶│ bom_assembly / footprints / config)      │    (area, layers, thickness,
   BOM        ──▶│                                           │     copper, finish, mask,
                 └──────────────────────────────────────────┘     min-feature→tier,
                                                                   joints, unique_parts, …)
                                     │
              PriceSpec + qty + vendor catalog (config)
                                     ▼
   ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
   │ OshParkModel  │  │  JlcpcbModel  │  │ PikkoloModel  │   ← each: price(spec, qty) -> Quote
   │ (fab)         │  │ (fab + asm)   │  │ (asm only)    │
   └───────────────┘  └───────────────┘  └───────────────┘
                                     ▼
                    Quote { line_items[], subtotal, shipping_note,
                            confidence, source_url, as_of }
```

- **`PriceSpec`** — the normalized, vendor-neutral board description (dataclass).
- **`PriceModel` Protocol** — `name`, `services: {"fab"|"assembly"}`, `capabilities` (max
  layers, min feature it can build → so an over-spec board is flagged, not silently priced),
  and `price(spec, qty) -> Quote`.
- **`Quote`** — itemized breakdown so the output can explain *why* a number is what it is,
  plus a `confidence` enum (`exact` | `estimate` | `manual`) and provenance.
- Read-only, stdlib-only, same exit-code contract as the rest of `kicad-bench`.

---

## 5. Per-vendor models (with live numbers)

### 5a. OSH Park — `confidence: exact`
Pure area × rate; you get **3 copies** per order (orders are multiples of 3). US shipping
historically free → shipping note ≈ $0 domestic.

| Service | Rate | Copies | Notes |
|---|---|---|---|
| 2-layer proto | **$5.00 / in²** | 3 | 1.6 mm, 1 oz, ENIG, purple mask; 6/6 mil, 13 mil drill, 7 mil ring |
| 4-layer proto | **$10.00 / in²** | 3 | |
| 4-layer Super Swift | **$20.00 / in²** | 3 | 5–6 business-day ship |
| 2-layer medium run | **$1.00 / in²** | — | **100 in² minimum** |
| 4-layer medium run | **$2.00 / in²** | — | 100 in² minimum |
| 2-layer 2oz / 0.8 mm | (special service) | | separate service tier |

```
price = area_in2 * rate[layers, service]
copies = 3 * ceil(qty / 3)          # you always get multiples of 3
```
Formula is exact; the only judgement is picking the service tier (proto vs medium-run vs
swift) from the requested qty + lead time.

**Shipping is NOT free** — corrected from the initial draft. A real order (§11) shipped
**FedEx 2 Day at $11.00** on an $11.60 board: on small proto boards shipping ≈ the board
cost, so it must be modeled, not assumed $0. Treat as a config `shipping[method] = flat`
estimate (FedEx 2 Day ≈ $11) with a note that it's a standing estimate, not a live rate.
That same order also showed **Super Swift at $0.00** — consistent with OSH Park now bundling
Super Swift into the 2-layer proto tier at no upcharge; the $20/in² swift rate applies to the
4-layer swift service.

### 5b. JLCPCB — `confidence: estimate`
Tiered base (area × qty) + per-option adders + engineering fee. Instant quotes advertised
"from $2". Model = a **base tier table** (area-band × qty-band → base price) plus additive
adders:

- **Layers** — 1/2-layer cheapest; 4-layer step; 6+ steeper.
- **Copper** — 1 oz default (free); 2 oz / ≥4.5 oz adders (longer etch/plate).
- **Thickness** — 1.6 mm standard; others may hit special-process. 0.40 mm forces ENIG.
- **Surface finish** — HASL default; **ENIG** charged when ENIG area > 30% of board:
  **$0.8992 / m² per 1%** over 30%.
- **Mask color** — green standard; non-green → special-process / engineering fee.
- **Min feature** — sub-standard trace/space/drill → "advanced" adder (drive off `fab_min`).
- **Impedance control** — flat adder when requested.
- **Engineering fee** — for any non-standard option bundle.

**Assembly (JLC):** setup fee (**~$8**, frequently coupon-waived for 1–6 layer), **per-unique-
part** handling/loading fee (Extended parts add a per-line loading fee; Basic/Preferred
don't), per-joint solder cost, stencil. Cost scales with **unique part count**, not raw
placement count — which we already compute in `bom_assembly.py`.

The base-tier table is the part most worth seeding from the live quote engine and dating.

### 5c. Pikkolo — `confidence: estimate` (assembly only)
**Upgraded from `manual` to `estimate` — calibrated against a real invoice (§11).** Erie, CO
turnkey assembly with a boutique, low-volume-friendly structure (the earlier "$0.10/placement,
$100–300 NRE" guess was wrong on both counts):

```
components = sum(part_unit_cost * qty) * (1 + 0.20) [+ applicable tariffs]   # Pikkolo sources parts
placement  = placements * per_placement_usd            # observed ≈ $0.348/placement
setup      = setup_usd                                 # observed $0.50 flat  (!)
assembly_total = components + placement + setup
```

Observed on a 6-placement order: components **$28.72** (incl. the 20% stocking fee), placement
**$2.09** (6 × ≈ $0.348), setup **$0.50** → **$31.31** total. Two calibration caveats (single
data point):
- Whether "Part Placement × 6" counts **total placements** or **unique parts** is unconfirmed —
  for this board they may coincide. Flag both interpretations until a second, higher-count
  order disambiguates.
- The **components line needs BOM part pricing** (unit cost × qty), which we can pull from the
  BOM / `lcsc-bom-picker` data — so Pikkolo assembly cost is only as good as the BOM's costed
  lines. Parts without a price → the components estimate is marked partial.
- Tariffs are order-/part-dependent and left out of the estimate (noted, not modeled).

---

## 6. Config schema (proposal)

New top-level `[pricing]` block in `kicad-bench.toml` pointing at a vendor catalog, so the
catalog is shareable across projects and the board only picks vendors + qty defaults. All
prices carry `source_url` + `as_of`.

```toml
[pricing]
catalog = "configs/fab_pricing.toml"   # shared, versioned vendor rate tables
vendors = ["oshpark", "jlcpcb", "pikkolo"]
default_qty = 10
region = "US"                          # shipping-note hint only
```

```toml
# configs/fab_pricing.toml  (excerpt)
[oshpark]
service = "fab"
confidence = "exact"
source_url = "https://docs.oshpark.com/services/"
as_of = "2026-07-16"
copies_multiple = 3
[[oshpark.rate]]  layers = 2  tier = "proto"       usd_per_in2 = 5.0
[[oshpark.rate]]  layers = 4  tier = "proto"       usd_per_in2 = 10.0
[[oshpark.rate]]  layers = 2  tier = "medium_run"  usd_per_in2 = 1.0   min_in2 = 100
[[oshpark.rate]]  layers = 4  tier = "medium_run"  usd_per_in2 = 2.0   min_in2 = 100
[oshpark.shipping]                      # NOT free — calibrated from a real order
fedex_2day_usd = 11.0                   # standing estimate, not a live rate

[jlcpcb]
service = "fab+assembly"
confidence = "estimate"
source_url = "https://jlcpcb.com/quote"
as_of = "2026-07-16"
engineering_fee_usd = 1.5
# base[area_band][qty_band] -> usd ; adders keyed by option
# … tiers + adders …
[jlcpcb.assembly]
setup_usd = 8.0
per_joint_usd = 0.0017
extended_part_loading_usd = 3.0   # per unique Extended part

[pikkolo]
service = "assembly"
confidence = "estimate"                 # calibrated from a real invoice, §11
source_url = "https://www.pikkoloassembly.com/"
as_of = "2026-07-16"
per_placement_usd = 0.348               # observed 6 placements → $2.09
setup_usd = 0.50                        # observed flat setup
component_markup_pct = 20               # parts sourced at cost + 20% stocking fee
# tariffs: order/part-dependent, not modeled (noted in output)
```

---

## 7. Provenance & confidence

Every `Quote` reports a `confidence`:
- **`exact`** — closed-form vendor formula (OSH Park).
- **`estimate`** — tiered/approximate; excludes shipping/promos (JLC).
- **`manual`** — config values the user must confirm (a vendor with no published or observed
  rates yet).

`kb price --refresh` re-fetches the public pricing pages and rewrites the catalog's rate
tables + `as_of` (OSH Park + JLC publish theirs; Pikkolo stays manual). A stale `as_of`
(> N days) prints a warning, mirroring the datasheet-provenance discipline already in the
repo. *(Note: the OSH Park and Pikkolo sites 403 a plain fetch; `--refresh` will need a
real UA/headers or a small per-vendor scraper — flagged as an implementation risk.)*

---

## 8. CLI / UX

```
$ kb price --qty 3
Board: interposer · 2-layer · 6 placements

FAB (bare board, qty 3 = 1 set)
  OSH Park    $11.60   exact      1 set of 3 + FedEx 2 Day ~$11.00
  JLCPCB      $ 4.00   estimate   + ship ~$18 (excl.)      [tier: standard]

ASSEMBLY (qty 3)
  Pikkolo     $31.31   estimate   components $28.72 (+20%) + 6×$0.348 + $0.50 setup
  JLCPCB      $—       estimate   (needs BOM costing)

BEST POPULATED COMBO
  OSH Park board + Pikkolo asm   ≈ $53.91   ← the user's actual interposer flow (§11)
  JLCPCB all-in                  ≈ $—       (+ ship, ex-promo)

$ kb price --stackup 2L_1oz          # re-cost a 2-layer respin
$ kb price --fab oshpark --qty 3     # single vendor, cheapest proto qty
$ kb price --json                    # machine-readable, for the sidecar tab
```

Integration points: a **`kb price` command**; a column/row in **`stackup-sync`** so cost is
visible while choosing a stackup; and a **sidecar** dashboard tab (`--json` feed).

---

## 9. Open questions (need your call before coding)

1. **Home for the code** — `kicad-bench` (project-agnostic, my default) vs a standalone skill?
2. **Assembly depth in v1** — full JLC assembly (unique-part tiers, Basic/Extended split) or
   just bare-board fab first and assembly in a follow-up?
3. **Pikkolo NRE** — do you have a recent quote I can pin, or ship the `manual` stub?
4. **Quantity model** — single qty, or a small price-break curve (e.g. 5/10/30/100)?
5. **Shipping** — omit entirely, or a coarse per-region flat estimate as a labeled line?
6. **Panelization** — assume 1-up in v1, or model v-cut/tab panels (affects area & JLC price)?

---

## 10. Phased build (once signed off)

- **P0 — spec extractor:** `board_bbox()` in `pcbgeom` + `PriceSpec` from `fab.config`/BOM.
  Unit-tested against the LVDS board. No pricing yet.
- **P1 — OSH Park (`exact`):** closed-form model + `kb price --fab oshpark`. Proves the
  pipeline end-to-end on a real board.
- **P2 — vendor catalog + JLC fab (`estimate`):** config schema, tier tables, adders,
  comparison table.
- **P3 — assembly:** JLC assembly + Pikkolo; populated-board combo rows.
- **P4 — provenance/refresh + sidecar tab + `stackup-sync` cost column.**

---

## 11. Calibration — real orders

Ground-truth anchors the estimator against. Each entry is a real invoice; the model is
"good" when it reproduces these within a stated tolerance.

### `interposer-v1-pikkolo` (2026-07-14) — populated interposer, OSH Park fab + Pikkolo asm
The user's actual two-vendor flow: **OSH Park fabs the bare board and ships it direct to
Pikkolo** (ship-to = `Pikkolo Assembly, 3336 Arapahoe Rd Unit B PMB 248, Erie CO 80516`),
Pikkolo populates it.

**OSH Park invoice** (order `sgFAiLi9`) — bare board:
| Line | Amount |
|---|---|
| PCB set cost ($11.60 / set of 3), qty 3 | $11.60 |
| Super Swift Service | $0.00 |
| Project subtotal | $11.60 |
| Shipping — FedEx 2 Day | $11.00 |
| **Total** | **$22.60** |

**Pikkolo invoice** — assembly (6 placements):
| Line | Amount |
|---|---|
| Components (+20% stocking fee / applicable tariffs) | $28.72 |
| Part Placement × 6 (≈ $0.348 ea) | $2.09 |
| Setup | $0.50 |
| Subtotal | $31.31 |
| Shipping | $0.00 |
| **Total** | **$31.31** |

**Populated board, end-to-end ≈ $53.91** (OSH Park $22.60 incl. ship-to-Pikkolo + Pikkolo
$31.31), before any final ship from Pikkolo to the user.

**What this calibration pinned / corrected:**
- OSH Park bare board is exactly `set_cost` per set of 3 → **$11.60/set** for this interposer
  (⇒ ≈ 2.3 in² at $5/in² 2-layer, or ≈ 1.16 in² at 4-layer — layer count TBC from the board).
- OSH Park **shipping is real** (~$11 FedEx 2 Day), not $0 — the single biggest fix.
- OSH Park **Super Swift bundled at $0.00** on 2-layer proto.
- Pikkolo: **$0.50 setup** (not $100–300), **≈$0.348/placement** (not $0.10), **components at
  cost + 20%**. Upgraded Pikkolo from `manual` → `estimate`.
- The **mix-vendor combo is the primary use case**, confirmed by the ship-to address.

*(TODO: add a JLC-all-in order and a higher-placement Pikkolo order to disambiguate
placements-vs-unique-parts and firm up the JLC tier table.)*

---

### Sources (fetched 2026-07-16)
- OSH Park services & pricing — https://docs.oshpark.com/services/ ,
  https://docs.oshpark.com/services/two-layer/ , https://docs.oshpark.com/services/four-layer/
- JLCPCB pricing breakdown — https://jlcpcb.com/blog/pcb-pricing-breakdown ,
  https://jlcpcb.com/help/article/pcb-assembly-price ,
  https://jlcpcb.com/help/article/in-what-cases-will-there-be-charged-extra
- Pikkolo Assembly — https://www.pikkoloassembly.com/
