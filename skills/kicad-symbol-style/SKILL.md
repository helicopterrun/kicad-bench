---
name: kicad-symbol-style
description: >
  The house style for how every KiCad schematic symbol in our libraries is drawn —
  pin grouping, field placement, typography, orientation, and the on-grid rule — so
  schematics read cleanly and review fast. Use when drawing or editing a symbol, when
  reviewing a new/changed library part, or before a symbol enters the library. The
  objective subset is enforced by `kb symbol-style`; the rest is the human reviewer's
  job. Imported from the claude.ai/design "KiCad Design Guide Creation" project
  (v0.1 draft; footprint half pending).
---

# KiCad Schematic Symbol Style Guide

How every symbol in our library should be drawn so schematics read at a glance and
review fast. Default KiCad output is the floor we are climbing out of. Source of truth:
this file; the rendered visual version lives in the claude.ai/design project
"KiCad Design Guide Creation".

## 1. Principles (these carry almost all the weight)

1. **Group pins by function, never by pin number.** Power, ground, signal buses, and
   mechanical pins each cluster together.
2. **No text may cross a pin, the body edge, or another field.** Overlap is the single
   biggest tell of a default symbol.
3. **Everything snaps to the 50 mil (1.27 mm) grid** — pins, fields, body lines. No
   off-grid endpoints, ever.
4. **Consistent orientation.** Passives horizontal, signal flowing left → right. Rotate
   only in 90° steps.
5. **Pin numbers are the contract with the footprint.** Every number on the symbol maps
   to a real pad.

## 2. The seven fields

| Field | Lives | Notes |
|---|---|---|
| Reference designator | above body (or per placement rule) | `J1 R1 C1`, auto-incremented |
| Value | paired with reference | part value / library name; **italic** |
| Pin name | **inside** the body, justified toward its pin | the signal function |
| Pin number | **above** the pin line, outside the body | the physical pad ID — must match the footprint |
| Electrical type | (editor only, hidden on sheet) | drives ERC — set it correctly |
| Description & datasheet | rotated, tucked **inside** the body | never reach the pins |

## 3. Typography — two roles, two sizes

| Role | Fields | Size | Justify | Style |
|---|---|---|---|---|
| Primary | reference, pin name, pin number | **50 mil (1.27 mm)** | center/center | default font, regular |
| Secondary | description, notes, datasheet | **40 mil (1.016 mm)** | center/center | default font, *italic* |

Default font = the built-in KiCad Font. No second typeface, no off-scale sizes.

> **House resolution of a draft ambiguity:** the guide lists Value under "primary"
> (50 mil regular) but also calls Value "always italic." Our existing
> `0_Project_Defaults` library draws Value at **40 mil italic**, so that is the house
> default the checker accepts. What is *not* negotiable: Value is **italic**.

## 4. Field placement

Fields may sit top / center / bottom and left / center / right of the body.
- **Side fields:** anchor on the edge nearest the symbol origin (0,0), offset **+100 mil
  (2.54 mm) horizontally** and **50 mil (1.27 mm) vertically** from center. Left-justify a
  right-side field; right-justify a left-side field (anchor faces the body).
- **Top/bottom fields:** center-justified above/below the body. Default pair = reference
  above, value below.
- **Never overlap.** If a field would touch a pin or another field, move it to the next
  free side.

## 5. Orientation & flow

Two-terminal passives default **horizontal**, current left → right, so the sheet reads
like a sentence. Rotation in 90° steps only; text stays readable (KiCad reads side text
bottom-to-top — never upside down). Pick one field arrangement per library and hold it.

## 6. Pin organisation

- **Power up top, ground directly below** — the reader finds the rails without scanning.
- **Repeated nets stack together** with pad numbers laddered (A4 / A9 / B4 / B9), not
  scattered by number.
- **Differential & config pairs adjacent** (D−/D−, D+/D+, CC1/CC2) so routing intent is
  obvious.
- **One empty grid slot between groups** — whitespace separates, not borders.
- **Mechanical pins (SHIELD, mounting) at the bottom edge** — present, out of the signal
  reading path.

## 7. Footprints

Pending — the imported guide has not yet codified the footprint half (courtyard, silk,
fab/assembly layers, pad↔pin numbering, pin-1 marker, 3D alignment, naming). Add real
examples to the claude.ai/design project, then extend this section + the checker.

## 8. Reviewer checklist (before a symbol enters the library)

| Check | Enforced by |
|---|---|
| Reference & value placed, on grid, no overlap | `kb symbol-style` (grid, sizes); overlap = human |
| Pins grouped by function, gap between groups | human review |
| Every pin number matches a footprint pad | human review (footprint half pending) |
| No text crosses a pin, body, or field | human review |
| Primary 50 mil regular, secondary 40 mil italic, value italic | `kb symbol-style` |
| Passive horizontal, flows left → right | human review (schematic placement) |
| Electrical types set for ERC | `kb symbol-style` (flags `unspecified`) |
| Datasheet & description filled, tucked inside | human review |

## How to run the gate

```sh
kb symbol-style [LIB.kicad_sym ...]   # default: all *.kicad_sym under the project
kb symbol-style --strict              # promote style warnings to failures (exit 1)
```

Advisory by default (warns, exit 0) because this guide is a v0.1 draft and existing
libraries predate it; use `--strict` once a library is migrated to make it a hard gate.
The checker covers the *objective* rules only (grid, typography, italic, pin-type
hygiene). Grouping, overlap, orientation, and pad↔pin matching remain human review.
