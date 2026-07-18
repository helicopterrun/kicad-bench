"""constraints — build & inspect the per-datasheet extracted-constraints store.

Subcommands:
  kb constraints status          what each BOM part / ingested datasheet has
  kb constraints seed [--force]  no-API pin-table seed via the heuristic extractor
  kb constraints extract [MPN]   LLM extraction (pin tables from pinout-page images,
                                 ratings from section text) — needs ANTHROPIC_API_KEY
  kb constraints show MPN        print the stored constraints for a part

`seed` writes ``extracted_by: "heuristic"`` files (pin table only — enough for the
pin-mux gate). `extract` writes ``extracted_by: "llm"`` files with abs-max /
recommended / typed specs, each field cited to its source pages. Both skip a
datasheet whose constraints.json is current (same PDF sha, same schema) unless
--force. The LLM dependency (`anthropic`) is the optional `llm` extra.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import datasheet as dsmod
from .core import config as cfgmod, conspec, graph as graphmod

DEFAULT_EXTRACT_MODEL = "claude-sonnet-5"
MAX_PINOUT_IMAGES = 4
IMAGE_DPI = 150
SMALL_DOC_CHARS = 40_000     # below this, send the whole datasheet text


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _bom_mpns(cfg: cfgmod.Config) -> list[str]:
    """Distinct MPNs on the schematic (via the design graph's component parse)."""
    g = graphmod.build(cfg.root_sch)
    seen: dict[str, None] = {}
    for ref in sorted(g.components):
        mpn = g.components[ref].mpn
        if mpn:
            seen.setdefault(mpn)
    return list(seen)


def _entries(cfg: cfgmod.Config) -> list[tuple[Path, dict]]:
    """(index_dir, manifest) for every ingested datasheet across index roots."""
    out: list[tuple[Path, dict]] = []
    for root in conspec.index_dirs(cfg):
        # _index_entries expects the *board* root; our roots are index dirs
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            ij = d / "index.json"
            if ij.is_file():
                try:
                    out.append((d, json.loads(ij.read_text())))
                except (OSError, json.JSONDecodeError):
                    continue
    return out


def _mpns_for(manifest: dict, bom_mpns: list[str]) -> list[str]:
    """The MPN list for a constraints file: the manifest's own + matching BOM MPNs."""
    mpns: dict[str, None] = {}
    own = manifest.get("mpn") or ""
    if own:
        mpns.setdefault(own)
    for bm in bom_mpns:
        if own and conspec.mpn_matches(bm, own):
            mpns.setdefault(bm)
    return list(mpns)


def _page_texts(index_dir: Path) -> list[str]:
    full = index_dir / "text" / "full.txt"
    if not full.is_file():
        return []
    return full.read_text(errors="replace").split("\f")


def _is_current(index_dir: Path, manifest: dict) -> bool:
    existing = conspec.load_dir(index_dir)
    return bool(existing and existing.get("datasheet_sha256") == manifest.get("sha256"))


def _select(entries: list[tuple[Path, dict]], query: str | None,
            bom_mpns: list[str]) -> list[tuple[Path, dict]]:
    if not query:
        return entries
    q = conspec.norm_mpn(query)
    out = [(d, m) for d, m in entries
           if q in conspec.norm_mpn(m.get("mpn") or "")
           or any(q in conspec.norm_mpn(x) for x in _mpns_for(m, bom_mpns))]
    if not out:
        sys.exit(f"error: no ingested datasheet matches {query!r}")
    return out


# ---------------------------------------------------------------------------
# status / show
# ---------------------------------------------------------------------------

def _cmd_status(cfg: cfgmod.Config, args) -> int:
    bom = _bom_mpns(cfg)
    entries = _entries(cfg)
    find = conspec.lookup(cfg)
    print(f"BOM: {len(bom)} MPN(s); ingested datasheets: {len(entries)}\n")
    n_cov = 0
    for mpn in bom:
        cons = find(mpn)
        if cons:
            n_cov += 1
            tag = cons.get("extracted_by", "?")
            npins = len(cons.get("pins", []))
            print(f"  ✓ {mpn:32s} {tag:9s} {npins} pin(s)")
        else:
            has_ds = any(conspec.mpn_matches(mpn, m.get("mpn") or "")
                         for _, m in entries)
            note = "datasheet ingested — run seed/extract" if has_ds else "no datasheet ingested"
            print(f"  · {mpn:32s} {note}")
    print(f"\n{n_cov}/{len(bom)} BOM MPN(s) have constraints")
    return 0


def _cmd_show(cfg: cfgmod.Config, args) -> int:
    cons = conspec.load_for_mpn(cfg, args.mpn)
    if not cons:
        sys.exit(f"error: no constraints found for {args.mpn!r}")
    print(json.dumps({k: v for k, v in cons.items() if k != "_dir"},
                     indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# seed (heuristic, no API)
# ---------------------------------------------------------------------------

def _cmd_seed(cfg: cfgmod.Config, args) -> int:
    bom = _bom_mpns(cfg)
    targets = _select(_entries(cfg), getattr(args, "mpn", None), bom)
    n_new = n_skip = n_fail = 0
    for index_dir, manifest in targets:
        if not args.force and _is_current(index_dir, manifest):
            n_skip += 1
            continue
        pages = _page_texts(index_dir)
        pinout = (manifest.get("sections") or {}).get("pinout", [])
        if not pages:
            n_fail += 1
            continue
        draft = dsmod.extract_pin_table(pages, pinout)
        # Slash-separated pin names ("PA2/UART2_TX/ADC1_IN2") carry the
        # alternate functions on many datasheets — split them out so the
        # pin-mux gate has something to chew on even before LLM extraction.
        pins = []
        for p in draft.get("pins", []):
            name = p.get("name", "")
            functions = [t for t in name.split("/")[1:] if t] if "/" in name else []
            pins.append({"number": p["number"], "name": name,
                         "functions": functions, "type": p.get("etype", "")})
        if not pins:
            # nothing extractable (non-component doc, or table not found) —
            # an empty constraints file would just mask "not ingested" status
            n_fail += 1
            continue
        conspec.save(index_dir, {
            "schema": conspec.SCHEMA_VERSION,
            "mpns": _mpns_for(manifest, bom),
            "datasheet_sha256": manifest.get("sha256"),
            "extracted_by": "heuristic",
            "model": None,
            "package": draft.get("package"),
            "pins": pins,
            "abs_max": [], "recommended": [], "spec": {},
            "source_pages": {"pins": pinout},
        })
        n_new += 1
        print(f"  seeded {index_dir.name}: {len(pins)} pin(s)")
    print(f"{n_new} seeded, {n_skip} current, {n_fail} with no extractable pin table")
    return 0


# ---------------------------------------------------------------------------
# extract (LLM)
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You are extracting machine-checkable electrical constraints from a component \
datasheet for use by deterministic schematic checks. Extract ONLY what the \
datasheet explicitly states — never infer, average, or guess a value. Every \
extracted item must cite the page number(s) it came from. For the pin table, \
read the pinout page images (text-layer tables are unreliable); include every \
alternate/peripheral function listed per pin. For ratings, prefer the absolute \
maximum ratings and recommended operating conditions tables. If a field is not \
stated in the provided pages, leave it out (empty list / omit optional field)."""

_RATING_ITEM = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "param": {"type": "string"},
        "value": {"type": "string"},
        "unit": {"type": "string"},
        "pins": {"type": "string"},
        "page": {"type": "integer"},
    },
    "required": ["param", "value", "unit", "pins", "page"],
}

_EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "package": {"type": ["string", "null"]},
        "pins": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "number": {"type": "string"},
                "name": {"type": "string"},
                "functions": {"type": "array", "items": {"type": "string"}},
                "type": {"type": "string"},
            },
            "required": ["number", "name", "functions", "type"],
        }},
        "abs_max": {"type": "array", "items": _RATING_ITEM},
        "recommended": {"type": "array", "items": _RATING_ITEM},
        "spec": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "v_rating": {"type": ["number", "null"]},
                "dielectric": {"type": ["string", "null"]},
                "vf": {"type": ["number", "null"]},
                "if_max": {"type": ["number", "null"]},
                "vin_max": {"type": ["number", "null"]},
                "vout": {"type": ["number", "null"]},
            },
            "required": ["v_rating", "dielectric", "vf", "if_max",
                         "vin_max", "vout"],
        },
        "source_pages": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "pins": {"type": "array", "items": {"type": "integer"}},
                "abs_max": {"type": "array", "items": {"type": "integer"}},
                "recommended": {"type": "array", "items": {"type": "integer"}},
                "spec": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["pins", "abs_max", "recommended", "spec"],
        },
    },
    "required": ["package", "pins", "abs_max", "recommended", "spec",
                 "source_pages"],
}


def extract_model(cfg: cfgmod.Config) -> str:
    raw = cfg.raw.get("llm", {}) if isinstance(cfg.raw, dict) else {}
    return os.environ.get("KB_EXTRACT_MODEL") or raw.get("extract_model") \
        or DEFAULT_EXTRACT_MODEL


def extract_backend(cfg: cfgmod.Config) -> str:
    raw = cfg.raw.get("llm", {}) if isinstance(cfg.raw, dict) else {}
    return (os.environ.get("KB_EXTRACT_BACKEND") or raw.get("backend")
            or "claude-code")


def _client():
    try:
        import anthropic
    except ImportError:
        sys.exit("error: the `anthropic` package is required for extraction — "
                 "pip install 'kicad-bench[llm]'")
    return anthropic.Anthropic()


def _text_pages_for(manifest: dict, pages: list[str]) -> list[int]:
    """1-based pages worth sending as text: ratings sections ± 1 page; whole
    doc when it's small."""
    total_chars = sum(len(p) for p in pages)
    if total_chars <= SMALL_DOC_CHARS:
        return list(range(1, len(pages) + 1))
    sections = manifest.get("sections") or {}
    want: set[int] = set()
    for key in ("abs-max", "typical-app"):
        for p in sections.get(key, []):
            want.update({p - 1, p, p + 1})
    want.update(sections.get("pinout", []))
    return sorted(p for p in want if 1 <= p <= len(pages))


def _pinout_images(cfg: cfgmod.Config, index_dir: Path,
                   manifest: dict) -> list[dict]:
    """Base64 PNG content blocks for the pinout pages (best effort)."""
    pdf_rel = manifest.get("pdf")
    root = index_dir.parent.parent      # <board_root>/.datasheet-index/<slug>
    pdf = (root / pdf_rel) if pdf_rel else None
    if not pdf or not pdf.is_file():
        return []
    cache = root / dsmod.CACHE_DIRNAME
    blocks: list[dict] = []
    for page in _pinout_page_nums(manifest):
        try:
            png = dsmod._render(pdf, page, IMAGE_DPI, cache)
        except Exception:
            continue
        data = base64.standard_b64encode(png.read_bytes()).decode()
        blocks.append({"type": "text", "text": f"[Pinout page {page}:]"})
        blocks.append({"type": "image",
                       "source": {"type": "base64", "media_type": "image/png",
                                  "data": data}})
    return blocks


def extract_one(client, model: str, cfg: cfgmod.Config, index_dir: Path,
                manifest: dict, bom: list[str]) -> dict:
    """One LLM extraction call -> saved constraints dict."""
    pages = _page_texts(index_dir)
    text_pages = _text_pages_for(manifest, pages)
    content: list[dict] = [{
        "type": "text",
        "text": f"Datasheet: {manifest.get('mpn')} "
                f"({manifest.get('page_count')} pages). Extract constraints for "
                f"MPN(s): {', '.join(_mpns_for(manifest, bom))}.",
    }]
    for p in text_pages:
        content.append({"type": "text",
                        "text": f"--- page {p} ---\n{pages[p - 1]}"})
    content.extend(_pinout_images(cfg, index_dir, manifest))

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": _EXTRACT_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema",
                                  "schema": _EXTRACT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = _finish_extraction(index_dir, manifest, bom, json.loads(text), model)
    u = response.usage
    print(f"  extracted {index_dir.name}: {len(data.get('pins', []))} pin(s), "
          f"{len(data.get('abs_max', []))} abs-max row(s) "
          f"[{u.input_tokens} in / {u.output_tokens} out tok]")
    return data


def _pinout_page_nums(manifest: dict) -> list[int]:
    """Pages worth rendering for the pin table: the classified pinout pages, or —
    when the section classifier found none — the first pages of the document
    (pin diagrams and signal-name tables almost always live up front)."""
    pages = (manifest.get("sections") or {}).get("pinout") or []
    if not pages:
        n = manifest.get("page_count") or 0
        pages = list(range(1, min(3, n) + 1))
    return pages[:MAX_PINOUT_IMAGES]


def _pinout_paths(index_dir: Path, manifest: dict) -> list[tuple[int, Path]]:
    """(page, rendered-PNG path) for the pinout pages — for backends that read
    image files themselves (claude-code) rather than taking base64 blocks."""
    pdf_rel = manifest.get("pdf")
    root = index_dir.parent.parent
    pdf = (root / pdf_rel) if pdf_rel else None
    if not pdf or not pdf.is_file():
        return []
    cache = root / dsmod.CACHE_DIRNAME
    out = []
    for page in _pinout_page_nums(manifest):
        try:
            out.append((page, dsmod._render(pdf, page, IMAGE_DPI, cache)))
        except Exception:
            continue
    return out


def _finish_extraction(index_dir: Path, manifest: dict, bom: list[str],
                       data: dict, model: str) -> dict:
    """Stamp provenance fields and save — shared by both extract backends."""
    data.update({
        "schema": conspec.SCHEMA_VERSION,
        "mpns": _mpns_for(manifest, bom),
        "datasheet_sha256": manifest.get("sha256"),
        "extracted_by": "llm",
        "model": model,
    })
    conspec.save(index_dir, data)
    return data


def extract_one_claude_code(cfg: cfgmod.Config, index_dir: Path, manifest: dict,
                            bom: list[str], model: str) -> dict:
    """One extraction via `claude -p --json-schema` (subscription auth).

    Ratings sections go inline as text; pinout pages go as rendered PNG paths
    the session reads itself (Read is the only allowed tool)."""
    pages = _page_texts(index_dir)
    parts = [f"Datasheet: {manifest.get('mpn')} ({manifest.get('page_count')} pages). "
             f"Extract constraints for MPN(s): {', '.join(_mpns_for(manifest, bom))}.",
             _EXTRACT_SYSTEM]
    for p in _text_pages_for(manifest, pages):
        parts.append(f"--- page {p} ---\n{pages[p - 1]}")
    pngs = _pinout_paths(index_dir, manifest)
    if pngs:
        parts.append("Rendered pinout page images — Read each of these files "
                     "(the text-layer tables above are unreliable for pin tables):")
        parts.extend(f"  page {pg}: {path}" for pg, path in pngs)
    proc = subprocess.run(
        ["claude", "-p", "\n\n".join(parts),
         "--json-schema", json.dumps(_EXTRACT_SCHEMA),
         "--allowedTools", "Read", "--strict-mcp-config",
         "--output-format", "json", "--model", model],
        capture_output=True, text=True, timeout=900)
    meta = json.loads(proc.stdout)
    if meta.get("is_error"):
        raise RuntimeError(str(meta.get("result", "claude run failed"))[:500])
    data = meta.get("structured_output")
    if data is None:
        data = json.loads(meta["result"])
    out = _finish_extraction(index_dir, manifest, bom, data, model)
    print(f"  extracted {index_dir.name}: {len(out.get('pins', []))} pin(s), "
          f"{len(out.get('abs_max', []))} abs-max row(s) "
          f"[${meta.get('total_cost_usd', 0):.3f}]")
    return out


def _cmd_extract(cfg: cfgmod.Config, args) -> int:
    bom = _bom_mpns(cfg)
    targets = _select(_entries(cfg), getattr(args, "mpn", None), bom)
    backend = args.backend or extract_backend(cfg)
    model = args.model or extract_model(cfg)
    if backend == "api":
        client = _client()
    elif backend == "claude-code":
        if not shutil.which("claude"):
            sys.exit("error: `claude` CLI not on PATH — install Claude Code or "
                     "use --backend api with an ANTHROPIC_API_KEY")
        client = None
    else:
        sys.exit(f"error: unknown extract backend {backend!r}")
    n_new = n_skip = n_fail = 0
    for index_dir, manifest in targets:
        existing = conspec.load_dir(index_dir)
        current = (existing and existing.get("extracted_by") == "llm"
                   and existing.get("datasheet_sha256") == manifest.get("sha256"))
        if current and not args.force:
            n_skip += 1
            continue
        try:
            if backend == "claude-code":
                extract_one_claude_code(cfg, index_dir, manifest, bom, model)
            else:
                extract_one(client, model, cfg, index_dir, manifest, bom)
            n_new += 1
        except Exception as e:            # per-datasheet isolation
            n_fail += 1
            print(f"  FAILED {index_dir.name}: {e}", file=sys.stderr)
    print(f"{n_new} extracted, {n_skip} current, {n_fail} failed "
          f"({backend}, model {model})")
    return 0 if n_fail == 0 else 1


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def run(args) -> int:
    cfg = cfgmod.load_or_exit(args.config, getattr(args, "board", None))
    return {"status": _cmd_status, "show": _cmd_show,
            "seed": _cmd_seed, "extract": _cmd_extract}[args.sub](cfg, args)


def add_parser(sub):
    p = sub.add_parser("constraints",
                       help="per-datasheet extracted-constraints store (status/seed/extract/show)")
    ssub = p.add_subparsers(dest="sub", required=True)

    st = ssub.add_parser("status", help="coverage: BOM MPNs vs stored constraints")
    sh = ssub.add_parser("show", help="print stored constraints for an MPN")
    sh.add_argument("mpn")
    sd = ssub.add_parser("seed", help="heuristic pin-table seed (no API key)")
    sd.add_argument("mpn", nargs="?", help="limit to datasheets matching this MPN")
    sd.add_argument("--force", action="store_true")
    ex = ssub.add_parser("extract",
                         help="LLM extraction (claude-code CLI by default; --backend api)")
    ex.add_argument("mpn", nargs="?", help="limit to datasheets matching this MPN")
    ex.add_argument("--force", action="store_true")
    ex.add_argument("--backend", choices=["claude-code", "api"],
                    help="model backend (default: [llm].backend or claude-code)")
    ex.add_argument("--model", help=f"override model (default {DEFAULT_EXTRACT_MODEL})")

    for x in (st, sh, sd, ex):
        x.add_argument("--board", help="which board (multi-board configs)")
        x.add_argument("--config", help=f"path to {cfgmod.CONFIG_NAME}")
    p.set_defaults(func=run)
