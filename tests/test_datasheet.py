"""datasheet harvest/ingest tests — pure logic + offline I/O (no network, no poppler).

The poppler-dependent extraction is covered by an end-to-end test guarded on pdftotext/
pdftoppm being present; everything else (URL resolution, %PDF magic check, BOM parsing,
symbol-field pairing, registry, canonical naming) runs with no external tools.
"""
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from kicad_bench import datasheet as ds
from kicad_bench.core import config as cfgmod, schparse


def _cfg(tmp_path: Path, toml: str = "[project]\nroot = \".\"\n") -> cfgmod.Config:
    (tmp_path / "kicad-bench.toml").write_text(toml)
    return cfgmod.load(tmp_path / "kicad-bench.toml")


# -- URL resolution priority ----------------------------------------------
def test_resolve_url_toml_override(tmp_path):
    cfg = _cfg(tmp_path, '[project]\nroot = "."\n\n[datasheets]\nTPS65150 = "https://x/ds.pdf"\n')
    url, src = ds._resolve_url("tps65150", cfg, {"parts": {}})
    assert url == "https://x/ds.pdf" and src == "toml-override"


def test_resolve_url_registry_wins(tmp_path):
    cfg = _cfg(tmp_path, '[project]\nroot = "."\n\n[datasheets]\nFOO = "https://toml/x.pdf"\n')
    reg = {"parts": {"FOO": {"source_url": "https://reg/y.pdf"}}}
    url, src = ds._resolve_url("foo", cfg, reg)
    assert url == "https://reg/y.pdf" and src == "registry"


def test_resolve_url_unknown_is_informational(tmp_path):
    cfg = _cfg(tmp_path)
    url, reason = ds._resolve_url("nope", cfg, {"parts": {}})
    assert url is None and "web-search" in reason and "--url" in reason


# -- %PDF magic-byte gate -------------------------------------------------
def test_download_rejects_non_pdf(tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<html>not a pdf</html>")
    with pytest.raises(ValueError, match="not a PDF"):
        ds._download(html.as_uri(), tmp_path / "out.pdf", tmp_path)


def test_download_accepts_pdf(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4\n...minimal...\n%%EOF")
    out = ds._download(src.as_uri(), tmp_path / "Datasheets" / "x.pdf", tmp_path)
    assert out.exists() and out.read_bytes().startswith(b"%PDF")


class _FakeResp:
    """Minimal urlopen() context-manager stand-in: serves `body` with a (possibly lying)
    Content-Length header so we can exercise the truncation guard without a network."""
    def __init__(self, body: bytes, content_length: int | None):
        self._buf = io.BytesIO(body)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return self._buf.read(n)


def test_download_rejects_truncated_body(tmp_path, monkeypatch):
    # Server promises 5000 bytes but the (proxy-capped) stream ends cleanly at far fewer.
    monkeypatch.setattr(ds.time, "sleep", lambda *_: None)   # no backoff delay in tests
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        return _FakeResp(b"%PDF-1.4 short", content_length=5000)
    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ds.http.client.IncompleteRead):
        ds._download("https://x/y.pdf", tmp_path / "Datasheets" / "y.pdf", tmp_path, attempts=3)
    assert calls["n"] == 3                                    # retried, then gave up
    assert not (tmp_path / "Datasheets" / "y.pdf").exists()   # never wrote a truncated PDF


def test_download_accepts_full_body_with_content_length(tmp_path, monkeypatch):
    body = b"%PDF-1.4\nfull body\n%%EOF"
    monkeypatch.setattr(ds.urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _FakeResp(body, len(body)))
    out = ds._download("https://x/y.pdf", tmp_path / "Datasheets" / "y.pdf", tmp_path)
    assert out.read_bytes() == body


# -- canonical download names (path safety) -------------------------------
def test_canonical_pdf_name():
    assert ds._canonical_pdf_name("TPS65150", "http://x/a.pdf") == "TPS65150.pdf"
    assert ds._canonical_pdf_name("a/../b c", "http://x/y.pdf") == "a_.._b_c.pdf"  # no slashes survive
    assert ds._canonical_pdf_name("", "http://x/sn74lvc.pdf") == "sn74lvc.pdf"


# -- BOM reading (stdlib xlsx fallback + column matching) ------------------
def _make_xlsx(path: Path, rows: list[list[str]]) -> None:
    """Write a minimal valid .xlsx (inline-shared-strings) for the stdlib reader."""
    strings: list[str] = []
    def sidx(s: str) -> int:
        if s not in strings:
            strings.append(s)
        return strings.index(s)
    sheet_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(row):
            col = chr(ord("A") + c)
            cells.append(f'<c r="{col}{r}" t="s"><v>{sidx(val)}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = f'<worksheet {NS}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    sst = (f'<sst {NS} count="{len(strings)}" uniqueCount="{len(strings)}">'
           + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    ctypes = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
              'package/2006/content-types">'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
              'officedocument.spreadsheetml.sheet.main+xml"/></Types>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def test_read_xlsx_rows_stdlib(tmp_path):
    bom = tmp_path / "bom.xlsx"
    _make_xlsx(bom, [["Comment", "Designator", "LCSC Part #", "Datasheet"],
                     ["TPS65150", "U1", "C1234", "https://x/ds.pdf"]])
    rows = ds._read_xlsx_rows_stdlib(bom)
    assert rows[0][0] == "Comment" and rows[1][2] == "C1234"
    entries = ds._bom_entries(rows)
    assert entries == [{"mpn": "", "lcsc": "C1234", "url": "https://x/ds.pdf", "value": "TPS65150"}]


def test_bom_entries_finds_mpn_column():
    rows = [["Manufacturer Part Number", "Qty"], ["LM2902", "3"], ["", ""]]
    assert ds._bom_entries(rows) == [{"mpn": "LM2902", "lcsc": "", "url": "", "value": ""}]


# -- symbol Datasheet-field pairing ---------------------------------------
def test_datasheet_fields_pairs_value_to_url(tmp_path):
    sch = tmp_path / "b.kicad_sch"
    sch.write_text("""
(symbol (lib_id "x:R")
  (property "Reference" "R1" (id 0))
  (property "Value" "10k" (id 1))
  (property "Datasheet" "~" (id 3)))
(symbol (lib_id "x:U")
  (property "Reference" "U1" (id 0))
  (property "Value" "TPS65150" (id 1))
  (property "Datasheet" "https://ti.com/tps65150.pdf" (id 3)))
""")
    fields = schparse.datasheet_fields([sch])
    assert fields == {"TPS65150": "https://ti.com/tps65150.pdf"}   # "~" placeholder dropped


# -- registry round-trip --------------------------------------------------
def test_registry_roundtrip(tmp_path):
    reg = ds._load_registry(tmp_path)
    assert reg == {"schema": 1, "parts": {}}
    reg["parts"]["U1"] = {"source_url": "http://x/y.pdf", "ingested": True}
    ds._save_registry(tmp_path, reg)
    assert ds._load_registry(tmp_path)["parts"]["U1"]["source_url"] == "http://x/y.pdf"
    # the index root self-gitignores figures + scratch
    gi = (tmp_path / ds.INDEX_DIRNAME / ".gitignore").read_text()
    assert "*/figures/" in gi and ".tmp/" in gi


# -- end-to-end ingest (needs poppler) ------------------------------------
def _make_pdf(path: Path, pages: list[str]) -> None:
    def esc(s): return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objs = {1: "<< /Type /Catalog /Pages 2 0 R >>",
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    cur = 4
    content_ids, page_ids = [], []
    for t in pages:
        stream = "BT /F1 11 Tf 50 740 Td 14 TL\n" + "".join(
            f"({esc(ln)}) Tj T*\n" for ln in t.split("\n")) + "ET"
        objs[cur] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        content_ids.append(cur); cur += 1
    for cid in content_ids:
        objs[cur] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     "/Resources << /Font << /F1 3 0 R >> >> "
                     f"/Contents {cid} 0 R >>")
        page_ids.append(cur); cur += 1
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs[2] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>"
    out = b"%PDF-1.4\n"; offs = {}
    for i in range(1, max(objs) + 1):
        offs[i] = len(out)
        out += f"{i} 0 obj\n".encode() + objs[i].encode() + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {max(objs)+1}\n".encode() + b"0000000000 65535 f \n"
    for i in range(1, max(objs) + 1):
        out += f"{offs[i]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {max(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(out)


poppler = pytest.mark.skipif(
    not (shutil.which("pdftotext") and shutil.which("pdftoppm")),
    reason="poppler-utils (pdftotext/pdftoppm) not installed")


@poppler
def test_ingest_end_to_end_and_idempotent(tmp_path):
    (tmp_path / "Datasheets").mkdir()
    pdf = tmp_path / "Datasheets" / "part.pdf"
    _make_pdf(pdf, [
        # A real TOC page matches >=3 section keywords, so _toc_pages drops it from the
        # section map — the keyword hits below are TOC leaders, not the real section pages.
        ("Table of Contents\n2 Pin Configuration . . . . . . . . 2\n"
         "3 Absolute Maximum Ratings . . . . 3\n4 Typical Application . . . . . 4\n"
         "5 Layout Guidelines . . . . . . . . 5"),
        "Pin Configuration and Functions\nFigure 2. LQFP48 package pinout",
        "Absolute Maximum Ratings\nVIN -0.3 to 40 V",
        "Typical Application\nFigure 4. Typical application circuit\nsoft-start ramp",
    ])
    root = tmp_path
    res = ds._ingest_one(pdf, root, source_url="http://x/part.pdf")
    man = res["manifest"]
    assert res["status"] == "ingested"
    assert man["packages"] == ["LQFP48"]
    assert man["sections"]["pinout"] == [2]
    assert any(f["kind"] == "pinout" and f["package"] == "LQFP48" for f in man["figures"])
    # committed artifacts exist; figure PNG rendered
    idir = ds._index_dir(root, pdf)
    assert (idir / "text" / "p002.txt").exists()
    assert (idir / "figures" / "pinout-LQFP48.png").exists()
    assert json.loads((idir / "index.json").read_text())["sha256"] == man["sha256"]
    # second ingest is a no-op (unchanged sha + tool_version)
    assert ds._ingest_one(pdf, root)["status"] == "skipped"
    # --force re-runs
    assert ds._ingest_one(pdf, root, force=True)["status"] == "ingested"


# -- pin-table extraction (datasheet -> scaffold-ready DRAFT pins) ---------
# A multi-package pin-description table in pdftotext -layout style (offsets matter):
# three package number columns, a multi-pin "6, 7" cell, an absent "—" cell, and NC rows.
_MULTI_PKG_PAGE = (
    "Pin Descriptions\n"
    "\n"
    "                  Pin Number\n"
    "                                              Pin Name              Function\n"
    "   SOT25          SOT89-5        SO-8\n"
    "     1              4             8           VIN          Input Voltage\n"
    "     2              2            6, 7         GND          Ground\n"
    "     3              3             5           EN           Chip Enable\n"
    "     —              1           2, 3, 4       NC           No Connection\n"
    "     5              5             1           VOUT         Output Voltage\n"
    "\n"
    "Functional Block Diagram\n"
)


def test_extract_pin_table_picks_package_column():
    r = ds.extract_pin_table([_MULTI_PKG_PAGE], [1], package="SOT25")
    assert [(p["number"], p["name"]) for p in r["pins"]] == [
        ("1", "VIN"), ("2", "GND"), ("3", "EN"), ("5", "VOUT")]   # NC absent (—) for SOT25
    assert r["package"] == "SOT25" and r["confidence"] == "medium"


def test_extract_pin_table_expands_multipin_and_nc():
    r = ds.extract_pin_table([_MULTI_PKG_PAGE], [1], package="SO-8")
    nums = {p["name"]: [q["number"] for q in r["pins"] if q["name"] == p["name"]]
            for p in r["pins"]}
    assert nums["GND"] == ["6", "7"]                 # "6, 7" expands to two pins
    assert nums["NC"] == ["2", "3", "4"]
    assert all(p["etype"] == "no_connect" for p in r["pins"] if p["name"] == "NC")
    assert next(p["etype"] for p in r["pins"] if p["name"] == "VIN") == "power_in"


def test_extract_pin_table_ambiguous_without_package():
    r = ds.extract_pin_table([_MULTI_PKG_PAGE], [1])
    assert r["confidence"] == "low"                  # multi-package + no pick → low
    assert any("package column" in w for w in r["warnings"])


def test_extract_pin_table_safe_decline():
    r = ds.extract_pin_table(["just prose, no pin table here at all"], [1])
    assert r["pins"] == [] and r["confidence"] == "none"


# -- part <-> ingested datasheet linkage + overview -----------------------
def test_link_manifest_by_lcsc_dir():
    entries = [(Path("x"), {"pdf": "Imported CAD/C16214/datasheet.pdf", "mpn": "datasheet"})]
    got = ds._link_manifest("SomePart", "", "C16214", "", entries, {"parts": {}})
    assert got is not None and got[1]["pdf"].endswith("C16214/datasheet.pdf")


def test_link_manifest_by_name_stem():
    entries = [(Path("y"), {"pdf": "Datasheets/ap2112k-3.3.pdf", "mpn": "ap2112k-3.3"})]
    got = ds._link_manifest("AP2112K-3.3TRG1", "", "", "", entries, {"parts": {}})
    assert got is not None and got[1]["mpn"] == "ap2112k-3.3"


def test_leader_toc_page_excluded_from_sections():
    # A TOC page naming "Pin Definitions" must NOT be picked as the pinout page; the real
    # heading page should win. _leader_toc_pages flags the dotted-leader TOC.
    pages = [
        "Contents\n"
        "1 Overview . . . . . . . . . . . . . 2\n"
        "2 Electrical . . . . . . . . . . . . 5\n"
        "3 Absolute Maximum Ratings . . . . . 8\n"
        "5 Pin Definitions . . . . . . . . . 27\n"
        "6 Package . . . . . . . . . . . . . 33\n",                 # page 1 = TOC
        "regular body text, nothing here",                          # page 2
        "5. Pin Definitions\nThe CP2102N QFN28 pin definitions follow.",  # page 3 = real
    ]
    assert ds._leader_toc_pages(pages) == {1}
    loc = ds._locate_pages(pages)
    toc = ds._toc_pages(loc) | ds._leader_toc_pages(pages)
    pinout = [p for p in loc["pinout"] if p not in toc]
    assert pinout == [3] and 1 not in pinout                         # TOC dropped, heading kept


@poppler
def test_ingest_renders_pinout_section_without_caption(tmp_path):
    """A pinout on a page with no 'Figure N. <pkg> pinout' caption is still pre-rendered
    from the detected pinout SECTION (named by page, no package)."""
    (tmp_path / "Datasheets").mkdir()
    pdf = tmp_path / "Datasheets" / "front.pdf"
    _make_pdf(pdf, ["Pin Assignments\nIN 1  OUT 5  (no figure caption on this page)"])
    man = ds._ingest_one(pdf, tmp_path)["manifest"]
    pin = [f for f in man["figures"] if f["kind"] == "pinout"]
    assert pin and pin[0]["page"] == 1 and "package" not in pin[0]
    assert pin[0]["path"] == "figures/pinout-p001.png"
    assert (ds._index_dir(tmp_path, pdf) / "figures" / "pinout-p001.png").exists()
