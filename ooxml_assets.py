"""Deterministic, dependency-free editable Office artifact generation."""

from __future__ import annotations

import random
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


FIXED_TIMESTAMP = (2000, 1, 1, 0, 0, 0)
CORE_CREATED = "2000-01-01T00:00:00Z"
THEME_ACCENTS = {
    "arcane": "7460DA",
    "ember": "DF4A34",
    "frost": "4593CB",
    "verdant": "36A66F",
}

NS_REL_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CORE = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
NS_EXTENDED = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
NS_DOC = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _text(value, limit=4000):
    normalized = " ".join(str(value or "").strip().split())
    xml_safe = "".join(
        character
        for character in normalized
        if ord(character) >= 0x20
        and not 0xD800 <= ord(character) <= 0xDFFF
        and ord(character) not in {0xFFFE, 0xFFFF}
    )
    return escape(xml_safe[:limit])


def _theme_accent(theme):
    return THEME_ACCENTS.get(str(theme or "").lower(), THEME_ACCENTS["arcane"])


def _xml(body):
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body


def _write_package(path, entries):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        if temporary.exists():
            temporary.unlink()
        with zipfile.ZipFile(
            temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, content in sorted(entries.items()):
                info = zipfile.ZipInfo(name, FIXED_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content.encode("utf-8"))
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _core_properties(title, description):
    return _xml(
        '<cp:coreProperties xmlns:cp="%s" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:title>%s</dc:title><dc:subject>Generated editable artifact</dc:subject>'
        '<dc:creator>Sonder</dc:creator><cp:lastModifiedBy>Sonder</cp:lastModifiedBy>'
        '<dc:description>%s</dc:description>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
        '</cp:coreProperties>'
        % (NS_CORE, _text(title), _text(description), CORE_CREATED, CORE_CREATED)
    )


def _app_properties(kind, count=1):
    extra = ""
    if kind == "spreadsheet":
        extra = (
            '<HeadingPairs><vt:vector size="2" baseType="variant">'
            '<vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant>'
            '<vt:variant><vt:i4>%d</vt:i4></vt:variant>'
            '</vt:vector></HeadingPairs><TitlesOfParts>'
            '<vt:vector size="1" baseType="lpstr"><vt:lpstr>Data</vt:lpstr>'
            '</vt:vector></TitlesOfParts>' % count
        )
    elif kind == "presentation":
        extra = "<Slides>%d</Slides><Notes>0</Notes><HiddenSlides>0</HiddenSlides>" % count
    return _xml(
        '<Properties xmlns="%s" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>Sonder</Application><AppVersion>1.0</AppVersion>%s'
        '</Properties>' % (NS_EXTENDED, extra)
    )


def _root_relationships(office_target):
    return _xml(
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="%s"/>'
        '<Relationship Id="rId2" Type="%s/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="%s/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
        % (NS_REL_PACKAGE, NS_REL_OFFICE, office_target, NS_REL_PACKAGE, NS_REL_OFFICE)
    )


def _word_paragraph(text, style=""):
    properties = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (
        properties,
        _text(text),
    )


def write_docx(path, title, brief, theme="arcane"):
    """Write a deterministic editable Word document."""
    accent = _theme_accent(theme)
    content_types = _xml(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    paragraphs = [
        _word_paragraph(title, "Title"),
        _word_paragraph("Generated editable brief", "Subtitle"),
        _word_paragraph("Overview", "Heading1"),
        _word_paragraph(brief),
        _word_paragraph("Deliverables", "Heading1"),
        _word_paragraph(
            "A cohesive %s concept with reusable writing, data, visual, audio, and model assets."
            % theme
        ),
        _word_paragraph("Validation", "Heading1"),
        _word_paragraph(
            "This document is an editable OOXML package generated locally without downloaded assets or third-party libraries."
        ),
        _word_paragraph("Provenance", "Heading1"),
        _word_paragraph("Generated by Sonder's deterministic in-house artifact forge."),
    ]
    document = _xml(
        '<w:document xmlns:w="%s" xmlns:r="%s"><w:body>%s'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080"/>'
        '</w:sectPr></w:body></w:document>'
        % (NS_DOC, NS_REL_OFFICE, "".join(paragraphs))
    )
    styles = _xml(
        '<w:styles xmlns:w="%s">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/><w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:rPr><w:b/><w:color w:val="%s"/><w:sz w:val="42"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:rPr><w:i/><w:color w:val="596275"/><w:sz w:val="24"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/></w:pPr>'
        '<w:rPr><w:b/><w:color w:val="%s"/><w:sz w:val="30"/></w:rPr></w:style>'
        '</w:styles>' % (NS_DOC, accent, accent)
    )
    document_rels = _xml(
        '<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/styles" '
        'Target="styles.xml"/></Relationships>' % (NS_REL_PACKAGE, NS_REL_OFFICE)
    )
    entries = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": _root_relationships("word/document.xml"),
        "docProps/app.xml": _app_properties("document"),
        "docProps/core.xml": _core_properties(title, brief),
        "word/_rels/document.xml.rels": document_rels,
        "word/document.xml": document,
        "word/styles.xml": styles,
    }
    _write_package(path, entries)


def _xlsx_cell(reference, value, style=0):
    style_attr = ' s="%d"' % style if style else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '<c r="%s"%s><v>%s</v></c>' % (reference, style_attr, value)
    return '<c r="%s" t="inlineStr"%s><is><t>%s</t></is></c>' % (
        reference,
        style_attr,
        _text(value),
    )


def write_xlsx(path, title, brief, seed=1337, theme="arcane"):
    """Write a deterministic editable Excel workbook with structured sample data."""
    rng = random.Random(int(seed))
    accent = _theme_accent(theme)
    clean_title = " ".join(str(title or "").strip().split())[:80]
    rows = [("Index", "Value", "Group", "Description")]
    for index in range(1, 13):
        rows.append(
            (
                index,
                round(rng.uniform(10.0, 99.0), 3),
                ("alpha", "beta", "gamma")[index % 3],
                "%s sample %d" % (clean_title, index),
            )
        )
    row_xml = []
    columns = "ABCD"
    for row_index, row in enumerate(rows, 1):
        cells = [
            _xlsx_cell("%s%d" % (columns[column], row_index), value, 1 if row_index == 1 else 0)
            for column, value in enumerate(row)
        ]
        row_xml.append('<row r="%d">%s</row>' % (row_index, "".join(cells)))
    sheet = _xml(
        '<worksheet xmlns="%s" xmlns:r="%s"><dimension ref="A1:D13"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/><cols>'
        '<col min="1" max="1" width="10" customWidth="1"/>'
        '<col min="2" max="3" width="14" customWidth="1"/>'
        '<col min="4" max="4" width="36" customWidth="1"/></cols>'
        '<sheetData>%s</sheetData><autoFilter ref="A1:D13"/></worksheet>'
        % (NS_SHEET, NS_REL_OFFICE, "".join(row_xml))
    )
    workbook = _xml(
        '<workbook xmlns="%s" xmlns:r="%s"><bookViews><workbookView/></bookViews>'
        '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>'
        '<definedNames/><calcPr calcId="191029"/></workbook>'
        % (NS_SHEET, NS_REL_OFFICE)
    )
    workbook_rels = _xml(
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="%s/styles" Target="styles.xml"/>'
        '</Relationships>' % (NS_REL_PACKAGE, NS_REL_OFFICE, NS_REL_OFFICE)
    )
    styles = _xml(
        '<styleSheet xmlns="%s"><fonts count="2">'
        '<font><sz val="11"/><color theme="1"/><name val="Aptos"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font>'
        '</fonts><fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF%s"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>' % (NS_SHEET, accent)
    )
    content_types = _xml(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    entries = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": _root_relationships("xl/workbook.xml"),
        "docProps/app.xml": _app_properties("spreadsheet"),
        "docProps/core.xml": _core_properties(title, brief),
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/styles.xml": styles,
        "xl/workbook.xml": workbook,
        "xl/worksheets/sheet1.xml": sheet,
    }
    _write_package(path, entries)


def _ppt_group_shape():
    return (
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
    )


def _ppt_text_shape(shape_id, name, text, x, y, width, height, size, color, bold=False):
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>'
        '<p:txBody><a:bodyPr wrap="square"/><a:lstStyle/><a:p><a:r>'
        '<a:rPr lang="en-US" sz="%d"%s><a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:rPr>'
        '<a:t>%s</a:t></a:r><a:endParaRPr lang="en-US" sz="%d"/></a:p></p:txBody></p:sp>'
        % (
            shape_id,
            _text(name),
            x,
            y,
            width,
            height,
            size,
            ' b="1"' if bold else "",
            color,
            _text(text),
            size,
        )
    )


def _ppt_slide(title, body, accent="7A6EE6"):
    title_shape = _ppt_text_shape(
        2, "Title", title, 685800, 571500, 10820400, 1143000, 3000, "F3F0FF", True
    )
    body_shape = _ppt_text_shape(
        3, "Body", body, 914400, 1943100, 10287000, 3200400, 1800, "D8D2F0"
    )
    accent_shape = (
        '<p:sp><p:nvSpPr><p:cNvPr id="4" name="Accent"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="685800" y="1714500"/><a:ext cx="2743200" cy="76200"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
        '<a:ln><a:noFill/></a:ln></p:spPr></p:sp>' % accent
    )
    return _xml(
        '<p:sld xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">'
        '<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="17152F"/></a:solidFill>'
        '<a:effectLst/></p:bgPr></p:bg><p:spTree>%s%s%s%s</p:spTree></p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
        % (NS_DRAWING, NS_REL_OFFICE, NS_PRESENTATION, _ppt_group_shape(), title_shape, body_shape, accent_shape)
    )


def _ppt_theme(accent):
    return _xml(
        '<a:theme xmlns:a="%s" name="Sonder Theme"><a:themeElements>'
        '<a:clrScheme name="Sonder"><a:dk1><a:srgbClr val="17152F"/></a:dk1>'
        '<a:lt1><a:srgbClr val="F3F0FF"/></a:lt1><a:dk2><a:srgbClr val="302A50"/></a:dk2>'
        '<a:lt2><a:srgbClr val="D8D2F0"/></a:lt2><a:accent1><a:srgbClr val="%s"/></a:accent1>'
        '<a:accent2><a:srgbClr val="55D9CF"/></a:accent2><a:accent3><a:srgbClr val="F3B45F"/></a:accent3>'
        '<a:accent4><a:srgbClr val="B78CFF"/></a:accent4><a:accent5><a:srgbClr val="66A9FF"/></a:accent5>'
        '<a:accent6><a:srgbClr val="8DD3A7"/></a:accent6><a:hlink><a:srgbClr val="66A9FF"/></a:hlink>'
        '<a:folHlink><a:srgbClr val="B78CFF"/></a:folHlink></a:clrScheme>'
        '<a:fontScheme name="Sonder"><a:majorFont><a:latin typeface="Aptos Display"/>'
        '<a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont>'
        '<a:latin typeface="Aptos"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="Sonder"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:gradFill rotWithShape="1"><a:gsLst><a:gs pos="0"><a:schemeClr val="phClr"/></a:gs>'
        '<a:gs pos="100000"><a:schemeClr val="phClr"><a:shade val="75000"/></a:schemeClr></a:gs></a:gsLst>'
        '<a:lin ang="5400000" scaled="0"/></a:gradFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:tint val="50000"/></a:schemeClr>'
        '</a:solidFill></a:fillStyleLst>'
        '<a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:prstDash val="solid"/></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:prstDash val="solid"/></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:prstDash val="solid"/></a:ln></a:lnStyleLst>'
        '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>'
        '<a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
        '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/></a:schemeClr></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"><a:tint val="85000"/></a:schemeClr></a:solidFill>'
        '</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'
        % (NS_DRAWING, accent)
    )


def write_pptx(path, title, brief, theme="arcane"):
    """Write a deterministic editable PowerPoint deck with three slides."""
    accent = _theme_accent(theme)
    slides = [
        (title, brief),
        (
            "Deliverables",
            "Editable writing • structured data • standalone UI • visual assets • audio • reusable 3D geometry",
        ),
        (
            "Validation & provenance",
            "Generated locally by Sonder • deterministic OOXML package • no downloaded assets • format-grounded before delivery",
        ),
    ]
    content_overrides = [
        '<Override PartName="/ppt/slides/slide%d.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        % index
        for index in range(1, len(slides) + 1)
    ]
    content_types = _xml(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '%s<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>' % "".join(content_overrides)
    )
    slide_ids = "".join(
        '<p:sldId id="%d" r:id="rId%d"/>' % (255 + index, index + 1)
        for index in range(1, len(slides) + 1)
    )
    presentation = _xml(
        '<p:presentation xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        '<p:sldIdLst>%s</p:sldIdLst><p:sldSz cx="12192000" cy="6858000" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/><p:defaultTextStyle/></p:presentation>'
        % (NS_DRAWING, NS_REL_OFFICE, NS_PRESENTATION, slide_ids)
    )
    presentation_relationships = [
        '<Relationship Id="rId1" Type="%s/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        % NS_REL_OFFICE
    ]
    presentation_relationships.extend(
        '<Relationship Id="rId%d" Type="%s/slide" Target="slides/slide%d.xml"/>'
        % (index + 1, NS_REL_OFFICE, index)
        for index in range(1, len(slides) + 1)
    )
    presentation_rels = _xml(
        '<Relationships xmlns="%s">%s</Relationships>'
        % (NS_REL_PACKAGE, "".join(presentation_relationships))
    )
    layout = _xml(
        '<p:sldLayout xmlns:a="%s" xmlns:r="%s" xmlns:p="%s" type="blank" preserve="1">'
        '<p:cSld name="Blank"><p:spTree>%s</p:spTree></p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
        % (NS_DRAWING, NS_REL_OFFICE, NS_PRESENTATION, _ppt_group_shape())
    )
    layout_rels = _xml(
        '<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/slideMaster" '
        'Target="../slideMasters/slideMaster1.xml"/></Relationships>'
        % (NS_REL_PACKAGE, NS_REL_OFFICE)
    )
    master = _xml(
        '<p:sldMaster xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">'
        '<p:cSld name="Sonder Master"><p:spTree>%s</p:spTree></p:cSld>'
        '<p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
        'accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" '
        'hlink="hlink" tx1="dk1" tx2="dk2"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        '<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>'
        % (NS_DRAWING, NS_REL_OFFICE, NS_PRESENTATION, _ppt_group_shape())
    )
    master_rels = _xml(
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="%s/theme" Target="../theme/theme1.xml"/>'
        '</Relationships>' % (NS_REL_PACKAGE, NS_REL_OFFICE, NS_REL_OFFICE)
    )
    entries = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": _root_relationships("ppt/presentation.xml"),
        "docProps/app.xml": _app_properties("presentation", len(slides)),
        "docProps/core.xml": _core_properties(title, brief),
        "ppt/_rels/presentation.xml.rels": presentation_rels,
        "ppt/presentation.xml": presentation,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": layout_rels,
        "ppt/slideLayouts/slideLayout1.xml": layout,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": master_rels,
        "ppt/slideMasters/slideMaster1.xml": master,
        "ppt/theme/theme1.xml": _ppt_theme(accent),
    }
    for index, (slide_title, slide_body) in enumerate(slides, 1):
        entries["ppt/slides/slide%d.xml" % index] = _ppt_slide(
            slide_title, slide_body, accent
        )
        entries["ppt/slides/_rels/slide%d.xml.rels" % index] = _xml(
            '<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/></Relationships>'
            % (NS_REL_PACKAGE, NS_REL_OFFICE)
        )
    _write_package(path, entries)
