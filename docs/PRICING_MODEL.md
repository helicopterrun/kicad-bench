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
| **Pikkolo** | ❌ | ✅ | Denver assembly house, **assembly only** (~$0.10/placement, low-volume, US turn). Pairs with someone else's bare board. |

**Consequence:** the model separates **fab** cost from **assembly** cost, and a *populated-
board* quote can combine two vendors (e.g. **OSH Park board + Pikkolo assembly**, or JLC for
both). The comparison output should therefore be able to show mix-and-match rows, not just
one vendor per line.

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

### 5c. Pikkolo — `confidence: manual` (assembly only)
Denver assembly, low-volume, ~**$0.10 / placement**, fast US turn / local pickup. Public
pricing is thin (setup/NRE not published), so v1 ships a **config stub** with the known
per-placement rate and a `TODO` setup fee for the user to fill from a real quote (or an
industry-standard $100–300 NRE placeholder, clearly flagged). Pairs with an OSH Park or JLC
bare board for a full populated-board estimate.

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
confidence = "manual"
source_url = "https://www.pikkoloassembly.com/"
as_of = "2026-07-16"
per_placement_usd = 0.10
setup_usd = 0        # TODO: confirm from a real quote
```

---

## 7. Provenance & confidence

Every `Quote` reports a `confidence`:
- **`exact`** — closed-form vendor formula (OSH Park).
- **`estimate`** — tiered/approximate; excludes shipping/promos (JLC).
- **`manual`** — config values the user must confirm (Pikkolo NRE).

`kb price --refresh` re-fetches the public pricing pages and rewrites the catalog's rate
tables + `as_of` (OSH Park + JLC publish theirs; Pikkolo stays manual). A stale `as_of`
(> N days) prints a warning, mirroring the datasheet-provenance discipline already in the
repo. *(Note: the OSH Park and Pikkolo sites 403 a plain fetch; `--refresh` will need a
real UA/headers or a small per-vendor scraper — flagged as an implementation risk.)*

---

## 8. CLI / UX

```
$ kb price --qty 10
Board: 42.0 × 58.0 mm (3.77 in²) · 4-layer · 1.6 mm · 1 oz · ENIG · 128 joints / 24 uniq

FAB (bare board, qty 10)
  OSH Park    $150.80   exact      → 12 boards (multiples of 3), US ship ~$0
  JLCPCB      $23.40    estimate   + ship ~$18 (excl.)      [tier: standard]

ASSEMBLY (qty 10)
  JLCPCB      $41.20    estimate   setup $8 + 24 uniq + 1280 joints
  Pikkolo     $128.00   manual     1280 placements × $0.10  (+ setup TODO)

BEST POPULATED COMBO
  OSH Park board + Pikkolo asm   $278.80   (US, no import)
  JLCPCB all-in                  $82.60    (+ ship, ex-promo)

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

### Sources (fetched 2026-07-16)
- OSH Park services & pricing — https://docs.oshpark.com/services/ ,
  https://docs.oshpark.com/services/two-layer/ , https://docs.oshpark.com/services/four-layer/
- JLCPCB pricing breakdown — https://jlcpcb.com/blog/pcb-pricing-breakdown ,
  https://jlcpcb.com/help/article/pcb-assembly-price ,
  https://jlcpcb.com/help/article/in-what-cases-will-there-be-charged-extra
- Pikkolo Assembly — https://www.pikkoloassembly.com/
