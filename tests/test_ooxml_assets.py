import hashlib
import zipfile
from xml.etree import ElementTree

import pytest

import ooxml_assets


CASES = [
    (
        "docx",
        ooxml_assets.write_docx,
        ("Launch & Learn", "Editable report with local provenance"),
        {"[Content_Types].xml", "_rels/.rels", "word/document.xml"},
        "word/document.xml",
    ),
    (
        "xlsx",
        ooxml_assets.write_xlsx,
        ("Launch & Learn", "Editable workbook with local provenance"),
        {
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/worksheets/sheet1.xml",
        },
        "xl/worksheets/sheet1.xml",
    ),
    (
        "pptx",
        ooxml_assets.write_pptx,
        ("Launch & Learn", "Editable deck with local provenance"),
        {
            "[Content_Types].xml",
            "_rels/.rels",
            "ppt/presentation.xml",
            "ppt/slides/slide1.xml",
        },
        "ppt/slides/slide1.xml",
    ),
]


@pytest.mark.parametrize("extension,writer,args,required,text_part", CASES)
def test_ooxml_packages_are_deterministic_safe_and_well_formed(
    tmp_path, extension, writer, args, required, text_part
):
    first = tmp_path / "first" / ("artifact." + extension)
    second = tmp_path / "second" / ("artifact." + extension)

    writer(first, *args)
    writer(second, *args)

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
    with zipfile.ZipFile(first) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        assert required <= set(names)
        assert len(names) == len(set(names))
        assert archive.testzip() is None
        assert all(item.date_time == ooxml_assets.FIXED_TIMESTAMP for item in infos)
        assert not any(name.startswith(('/', '\\')) or ".." in name.split("/") for name in names)
        for name in names:
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))
        rendered = " ".join(ElementTree.fromstring(archive.read(text_part)).itertext())
        assert "Launch & Learn" in rendered


def test_xlsx_contains_editable_rows_and_pptx_contains_three_slides(tmp_path):
    workbook = tmp_path / "workbook.xlsx"
    deck = tmp_path / "deck.pptx"
    ooxml_assets.write_xlsx(workbook, "Metrics", "Sample data", seed=42)
    ooxml_assets.write_pptx(deck, "Roadmap", "Three-stage delivery")

    with zipfile.ZipFile(workbook) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(
            ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"
        )
        assert len(rows) == 13
    with zipfile.ZipFile(deck) as archive:
        slides = [
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        ]
        assert len(slides) == 3


def test_ooxml_text_is_xml_safe_and_theme_accent_is_shared(tmp_path):
    document = tmp_path / "report.docx"
    workbook = tmp_path / "workbook.xlsx"
    deck = tmp_path / "deck.pptx"
    ooxml_assets.write_docx(document, "Frost\x01 Report", "Safe & editable", "frost")
    ooxml_assets.write_xlsx(workbook, "Frost\x01 Data", "Safe & editable", 42, "frost")
    ooxml_assets.write_pptx(deck, "Frost\x01 Deck", "Safe & editable", "frost")

    targets = [
        (document, "word/styles.xml"),
        (workbook, "xl/styles.xml"),
        (deck, "ppt/theme/theme1.xml"),
    ]
    for path, entry in targets:
        with zipfile.ZipFile(path) as archive:
            content = archive.read(entry)
            assert b"4593CB" in content
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    xml = archive.read(name)
                    assert b"\x01" not in xml
                    ElementTree.fromstring(xml)
