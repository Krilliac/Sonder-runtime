"""Bounded-memory and sheet-pairing regression tests for document_extract.

Covers the XLSX extraction blow-up (unbounded shared strings x 100-wide rows
joined before max_chars truncation), the same running-budget requirement for the
DOCX and PPTX paths, and XLSX sheet-name pairing with 10+ worksheets.
"""
from __future__ import annotations

import json
from pathlib import Path
import tracemalloc
import zipfile

import pytest

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.inspection import document_extract

_NS_X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

BIG = 20_000  # chars in one oversized string
MAX_CHARS = 1_000


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(file_ops, "allowed_roots", lambda _extra="": [root])
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def _make_big_shared_string_xlsx(path: Path, rows: int, cols: int) -> None:
    """One large shared string referenced by every cell of ``rows`` x ``cols``."""
    sheet_rows = []
    for r in range(1, rows + 1):
        cells = "".join(
            '<c r="%s%d" t="s"><v>0</v></c>' % (chr(ord("A") + c), r) for c in range(cols)
        )
        sheet_rows.append('<row r="%d">%s</row>' % (r, cells))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="%s"><sheets>'
            '<sheet name="Big" sheetId="1"/></sheets></workbook>' % _NS_X,
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="%s"><si><t>%s</t></si></sst>'
            % (_NS_X, "A" * BIG),
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
            % (_NS_X, "".join(sheet_rows)),
        )


def test_xlsx_large_shared_string_output_bounded(sandbox_root: Path):
    rows, cols = 60, 20
    p = sandbox_root / "big.xlsx"
    _make_big_shared_string_xlsx(p, rows, cols)

    result = document_extract.extract_document(str(p), max_chars=MAX_CHARS)

    assert len(result["text"]) <= MAX_CHARS
    assert result["truncated"] is True
    # No single returned cell may exceed the caller's character budget.
    for sheet in result["sheets"]:
        for row in sheet["rows"]:
            for cell in row:
                assert len(cell) <= MAX_CHARS
    # The whole payload (not just "text") stays small: unbounded it is ~24 MB.
    assert len(json.dumps(result)) < 200_000


def test_xlsx_row_building_stops_once_char_budget_reached(
    sandbox_root: Path, monkeypatch: pytest.MonkeyPatch
):
    rows, cols = 60, 20
    p = sandbox_root / "big.xlsx"
    _make_big_shared_string_xlsx(p, rows, cols)

    calls = {"n": 0}
    real = document_extract._col_ref_to_index

    def counting(cell_ref: str) -> int:
        calls["n"] += 1
        return real(cell_ref)

    monkeypatch.setattr(document_extract, "_col_ref_to_index", counting)
    document_extract.extract_document(str(p), max_chars=MAX_CHARS)

    total_cells = rows * cols
    # Budget is 1000 chars and every cell is 20000 chars wide: at most the first
    # row may be touched, never the full 1200 cells.
    assert calls["n"] <= 2 * cols, (
        "processed %d of %d cells; row building did not stop at max_chars"
        % (calls["n"], total_cells)
    )


def test_xlsx_large_shared_string_peak_memory_bounded(sandbox_root: Path):
    p = sandbox_root / "big.xlsx"
    _make_big_shared_string_xlsx(p, 60, 20)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        document_extract.extract_document(str(p), max_chars=MAX_CHARS)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Unbounded behaviour builds ~24 MB of joined row text (60 rows x 20 cells x 20k).
    assert peak < 4 * 1024 * 1024, "peak traced memory %d bytes" % peak


def _make_multi_sheet_xlsx(
    path: Path,
    count: int,
    *,
    with_rels: bool,
    rid_to_file: dict[int, int] | None = None,
) -> None:
    """``count`` sheets named S1..Sn; sheet k's only cell holds the text ``data-k``.

    ``rid_to_file`` maps sheet position k -> worksheets/sheet<N>.xml number; the
    default is the identity (the layout Excel writes).
    """
    mapping = rid_to_file or {k: k for k in range(1, count + 1)}
    # Cell text for sheet position k lives in worksheet file mapping[k].
    with zipfile.ZipFile(path, "w") as zf:
        sheets_xml = "".join(
            '<sheet name="S%d" sheetId="%d" r:id="rId%d"/>' % (k, k, k)
            for k in range(1, count + 1)
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>'
            % (_NS_X, _NS_R, sheets_xml),
        )
        if with_rels:
            rels = "".join(
                '<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
                % (k, _NS_R, mapping[k])
                for k in range(1, count + 1)
            )
            zf.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0"?><Relationships xmlns="%s">%s</Relationships>'
                % (_NS_REL, rels),
            )
        for k in range(1, count + 1):
            zf.writestr(
                "xl/worksheets/sheet%d.xml" % mapping[k],
                '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>'
                '<row r="1"><c r="A1" t="inlineStr"><is><t>data-%d</t></is></c></row>'
                "</sheetData></worksheet>" % (_NS_X, k),
            )


def test_xlsx_eleven_sheets_label_data_correctly(sandbox_root: Path):
    p = sandbox_root / "eleven.xlsx"
    _make_multi_sheet_xlsx(p, 11, with_rels=True)

    result = document_extract.extract_document(str(p))

    assert result["sheet_count"] == 11
    by_name = {s["name"]: s["rows"] for s in result["sheets"]}
    assert set(by_name) == {"S%d" % k for k in range(1, 12)}
    for k in range(1, 12):
        assert by_name["S%d" % k] == [["data-%d" % k]], "sheet S%d mislabelled" % k


def test_xlsx_eleven_sheets_without_rels_label_data_correctly(sandbox_root: Path):
    # No workbook rels part: fallback ordering must still be numeric, not lexicographic.
    p = sandbox_root / "eleven_norels.xlsx"
    _make_multi_sheet_xlsx(p, 11, with_rels=False)

    result = document_extract.extract_document(str(p))

    by_name = {s["name"]: s["rows"] for s in result["sheets"]}
    for k in range(1, 12):
        assert by_name["S%d" % k] == [["data-%d" % k]], "sheet S%d mislabelled" % k


def test_xlsx_sheet_names_follow_workbook_rels_not_file_numbering(sandbox_root: Path):
    # The OOXML mapping from <sheet r:id> to a worksheet part is the rels target;
    # file numbers need not match sheet order (e.g. after sheets are reordered).
    p = sandbox_root / "reordered.xlsx"
    _make_multi_sheet_xlsx(p, 3, with_rels=True, rid_to_file={1: 3, 2: 1, 3: 2})

    result = document_extract.extract_document(str(p))

    by_name = {s["name"]: s["rows"] for s in result["sheets"]}
    assert by_name == {
        "S1": [["data-1"]],
        "S2": [["data-2"]],
        "S3": [["data-3"]],
    }


def test_docx_running_budget_stops_paragraph_building(sandbox_root: Path):
    total = 300
    paras = "".join(
        "<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % ("P" * BIG) for _ in range(total)
    )
    p = sandbox_root / "big.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="%s"><w:body>%s</w:body></w:document>'
            % (_NS_W, paras),
        )

    # max_items is raised so the item cap alone cannot stop paragraph collection.
    result = document_extract.extract_document(str(p), max_chars=MAX_CHARS, max_items=1000)

    assert len(result["text"]) <= MAX_CHARS
    assert result["truncated"] is True
    # Paragraph accumulation must stop once the budget is spent, not collect all 300.
    assert result["paragraph_count"] < total


def test_docx_table_cells_bounded_by_char_budget(sandbox_root: Path):
    cell = "<w:tc><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:tc>" % ("T" * BIG)
    rows = "".join("<w:tr>%s</w:tr>" % (cell * 3) for _ in range(5))
    p = sandbox_root / "bigtable.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="%s"><w:body><w:tbl>%s</w:tbl></w:body></w:document>'
            % (_NS_W, rows),
        )

    result = document_extract.extract_document(str(p), max_chars=MAX_CHARS)

    for table in result["tables"]:
        for row in table:
            for c in row:
                assert len(c) <= MAX_CHARS


def test_pptx_running_budget_bounds_slides(sandbox_root: Path):
    total = 30
    p = sandbox_root / "big.pptx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, total + 1):
            zf.writestr(
                "ppt/slides/slide%d.xml" % i,
                '<?xml version="1.0"?><p:sld xmlns:p="%s" xmlns:a="%s">'
                "<a:t>Title%d</a:t><a:t>%s</a:t></p:sld>" % (_NS_P, _NS_A, i, "B" * BIG),
            )
            zf.writestr(
                "ppt/notesSlides/notesSlide%d.xml" % i,
                '<?xml version="1.0"?><p:notes xmlns:p="%s" xmlns:a="%s">'
                "<a:t>%s</a:t></p:notes>" % (_NS_P, _NS_A, "N" * BIG),
            )

    result = document_extract.extract_document(str(p), max_chars=MAX_CHARS)

    assert len(result["text"]) <= MAX_CHARS
    assert result["truncated"] is True
    for slide in result["slides"]:
        assert len(slide["text"]) <= MAX_CHARS
        assert len(slide["notes"]) <= MAX_CHARS
    # Slide building stops once the budget is spent instead of materialising all 30.
    assert len(result["slides"]) < total
