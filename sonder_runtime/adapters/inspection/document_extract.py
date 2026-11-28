"""Guarded, bounded, stdlib-only extraction for Office, web, SVG, and subtitle documents.

Supports:
- ``.docx`` (OOXML Word): core metadata, paragraphs, headings, tables.
- ``.xlsx`` (OOXML Excel): sheet names, dimensions, bounded cell rows.
- ``.pptx`` (OOXML PowerPoint): slide titles, body text, speaker notes.
- ``.html`` / ``.htm``: title, headings, visible text, links.
- ``.svg``: dimensions/viewBox, text nodes, element tag counts.
- ``.srt`` / ``.vtt``: cue count, time span, transcript text.

Security invariants:
- Every target passes ``file_ops.require_read_access`` and reparse-point checks.
- Sensitive repository/secret directories are refused even inside allowed roots.
- OOXML ZIP archives enforce bounded entry counts and cumulative decompressed
  byte ceilings (zip-bomb defense) and disallow DTD/entity expansion via stdlib
  ElementTree + explicit ``<!DOCTYPE`` / ``<!ENTITY`` rejection.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops


SUPPORTED_FORMATS = frozenset({"docx", "xlsx", "pptx", "html", "svg", "srt", "vtt"})
_SUFFIX_MAP = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".svg": "svg",
    ".srt": "srt",
    ".vtt": "vtt",
}

MAX_FILE_BYTES = 24 * 1024 * 1024
MAX_ZIP_ENTRIES = 512
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CHARS = 32_000
HARD_MAX_CHARS = 128_000
DEFAULT_MAX_ITEMS = 200
HARD_MAX_ITEMS = 1_000
_ENTITY_DECL_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_SRT_TIME_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
_VTT_TIME_RE = re.compile(
    r"^\s*((?:\d{2}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*((?:\d{2}:)?\d{2}:\d{2}\.\d{3})"
)
_XML_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_XML_NS_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XML_NS_DC = "{http://purl.org/dc/elements/1.1/}"
_XML_NS_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_XML_NS_DCTERMS = "{http://purl.org/dc/terms/}"
_XML_NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XML_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_XLSX_WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
_XLSX_WORKSHEET_RE = re.compile(r"xl/worksheets/[^/]+\.xml")
_XLSX_NUMBERED_WORKSHEET_RE = re.compile(r"xl/worksheets/sheet\d+\.xml")
_XLSX_MAX_SHEETS = 20


class DocumentExtractError(ValueError):
    """Stable rejection from bounded document extraction."""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _resolve_target(
    path: str,
    *,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> Path:
    requested = file_ops._requested_path(str(path or ""))
    if file_ops._is_reparse_point(requested):
        raise DocumentExtractError("document target must not be a symlink or junction")
    try:
        target = file_ops.require_read_access(
            str(path or ""),
            extra_roots=extra_roots,
            bypass=bypass,
            developer_authorized=developer_authorized,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise DocumentExtractError("document path rejected: %s" % exc) from exc
    if not target.exists() or not target.is_file():
        raise DocumentExtractError("document target must be an existing regular file")
    for root in file_ops.allowed_roots(extra_roots if bypass else ""):
        resolved_root = file_ops._resolve_best_effort(root)
        if target == resolved_root or file_ops._is_inside(target, resolved_root):
            relative = target.relative_to(resolved_root)
            if any(
                part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES
                for part in relative.parts
            ):
                raise DocumentExtractError(
                    "document path is secret or repository control state"
                )
            break
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise DocumentExtractError(
            "document exceeds maximum size ceiling (%d > %d bytes)" % (size, MAX_FILE_BYTES)
        )
    return target


def _join_capped(parts: Any, limit: int, sep: str = "") -> tuple[str, bool]:
    """Join string ``parts`` with ``sep`` into at most ``limit`` characters.

    Stops consuming ``parts`` once the limit is reached, so an oversized input
    never materialises a joined string larger than ``limit``. Returns the text
    and whether anything was cut.
    """
    pieces: list[str] = []
    total = 0
    for part in parts:
        if not part and not sep:
            continue
        extra = len(sep) if pieces else 0
        room = limit - total - extra
        if room <= 0:
            return sep.join(pieces), True
        piece = part[:room]
        pieces.append(piece)
        total += extra + len(piece)
        if len(piece) < len(part):
            return sep.join(pieces), True
    return sep.join(pieces), False


def _safe_xml_frombytes(raw: bytes) -> ET.Element:
    if _ENTITY_DECL_RE.search(raw[:8192]):
        raise DocumentExtractError("XML document declares DOCTYPE or ENTITY; refused")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DocumentExtractError("malformed XML payload: %s" % exc) from exc


def _read_zip_member(
    zf: zipfile.ZipFile,
    name: str,
    budget: list[int],
) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > budget[0]:
        raise DocumentExtractError("decompressed archive member exceeds safety ceiling")
    chunks = []
    remaining = budget[0]
    with zf.open(info, "r") as handle:
        while True:
            chunk = handle.read(min(65536, remaining + 1))
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                raise DocumentExtractError("decompressed archive exceeds safety ceiling")
            chunks.append(chunk)
    budget[0] = remaining
    return b"".join(chunks)


def _open_ooxml(target: Path) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(target, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise DocumentExtractError("invalid OOXML archive: %s" % exc) from exc
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        zf.close()
        raise DocumentExtractError("OOXML archive exceeds entry limit (%d)" % MAX_ZIP_ENTRIES)
    return zf


def _extract_core_metadata(zf: zipfile.ZipFile, budget: list[int]) -> dict[str, str]:
    meta: dict[str, str] = {}
    if "docProps/core.xml" not in zf.namelist():
        return meta
    root = _safe_xml_frombytes(_read_zip_member(zf, "docProps/core.xml", budget))
    tag_map = {
        f"{_XML_NS_DC}title": "title",
        f"{_XML_NS_DC}creator": "creator",
        f"{_XML_NS_DC}subject": "subject",
        f"{_XML_NS_DC}description": "description",
        f"{_XML_NS_CP}keywords": "keywords",
        f"{_XML_NS_DCTERMS}created": "created",
        f"{_XML_NS_DCTERMS}modified": "modified",
    }
    for elem in root.iter():
        key = tag_map.get(elem.tag)
        if key and elem.text and elem.text.strip():
            meta[key] = elem.text.strip()
    return meta


def _extract_docx(target: Path, max_chars: int, max_items: int) -> dict[str, Any]:
    budget = [MAX_DECOMPRESSED_BYTES]
    with _open_ooxml(target) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise DocumentExtractError("DOCX archive is missing word/document.xml")
        metadata = _extract_core_metadata(zf, budget)
        root = _safe_xml_frombytes(_read_zip_member(zf, "word/document.xml", budget))

    paragraphs: list[str] = []
    headings: list[str] = []
    tables: list[list[list[str]]] = []
    truncated = False
    # Running character budgets: paragraph text and table cell text each stop
    # being collected once ``max_chars`` characters have been gathered.
    para_chars = 0
    table_chars = 0
    para_budget_spent = False
    table_budget_spent = False

    body = root.find(f"{_XML_NS_W}body")
    children = list(body) if body is not None else list(root)
    for child in children:
        if para_budget_spent and table_budget_spent:
            truncated = True
            break
        if child.tag == f"{_XML_NS_W}p":
            if para_budget_spent:
                continue
            if len(paragraphs) >= max_items:
                truncated = True
                continue
            if para_chars >= max_chars:
                para_budget_spent = True
                truncated = True
                continue
            line, cut = _join_capped(
                (t.text for t in child.iter(f"{_XML_NS_W}t") if t.text), max_chars
            )
            line = line.strip()
            if not line:
                continue
            truncated = truncated or cut
            paragraphs.append(line)
            para_chars += len(line) + (1 if len(paragraphs) > 1 else 0)
            p_style = child.find(f"{_XML_NS_W}pPr/{_XML_NS_W}pStyle")
            if p_style is not None:
                val = str(p_style.attrib.get(f"{_XML_NS_W}val", ""))
                if val.lower().startswith("heading") or val.lower() == "title":
                    headings.append(line)
        elif child.tag == f"{_XML_NS_W}tbl":
            if table_budget_spent:
                continue
            if len(tables) >= max(20, max_items // 5):
                truncated = True
                continue
            rows: list[list[str]] = []
            for tr in child.findall(f"{_XML_NS_W}tr"):
                if len(rows) >= max_items:
                    truncated = True
                    break
                row_cells: list[str] = []
                for tc in tr.findall(f"{_XML_NS_W}tc"):
                    if table_chars >= max_chars:
                        table_budget_spent = True
                        truncated = True
                        break
                    cell_text, cut = _join_capped(
                        (t.text for t in tc.iter(f"{_XML_NS_W}t") if t.text),
                        max_chars - table_chars,
                    )
                    cell_text = cell_text.strip()
                    truncated = truncated or cut
                    table_chars += len(cell_text)
                    row_cells.append(cell_text)
                if any(row_cells):
                    rows.append(row_cells)
                if table_budget_spent:
                    break
            if rows:
                tables.append(rows)

    full_text = "\n".join(paragraphs)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
        truncated = True
    return {
        "metadata": metadata,
        "paragraph_count": len(paragraphs),
        "heading_count": len(headings),
        "headings": headings[:50],
        "table_count": len(tables),
        "tables": tables[:10],
        "text": full_text,
        "truncated": truncated,
    }


def _col_ref_to_index(cell_ref: str) -> int:
    col = 0
    for ch in cell_ref:
        if ch.isalpha():
            col = col * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return max(0, col - 1)


def _resolve_workbook_rel_target(target: str) -> str:
    """Resolve a ``xl/_rels/workbook.xml.rels`` Target to a ZIP member name."""
    path = target[1:] if target.startswith("/") else "xl/" + target
    return posixpath.normpath(path)


def _xlsx_sheet_entries(
    zf: zipfile.ZipFile,
    names: set[str],
    budget: list[int],
    workbook_sheets: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return ``(sheet name, worksheet member)`` pairs in workbook order.

    Pairing follows the OOXML relationship from each ``<sheet r:id>`` to its
    worksheet part. Worksheet files no sheet references are appended under their
    file stem. Without a usable rels part, numbered worksheet files are paired
    with workbook sheet names by position in numeric (not lexicographic) order.
    """
    numbered = sorted(
        (n for n in names if _XLSX_NUMBERED_WORKSHEET_RE.fullmatch(n)),
        key=_slide_sort_key,
    )
    rel_targets: dict[str, str] = {}
    if _XLSX_WORKBOOK_RELS in names:
        rels_root = _safe_xml_frombytes(_read_zip_member(zf, _XLSX_WORKBOOK_RELS, budget))
        for rel in rels_root.iter(f"{_XML_NS_PKG_REL}Relationship"):
            if str(rel.attrib.get("TargetMode", "")).lower() == "external":
                continue
            rel_id = rel.attrib.get("Id", "")
            rel_target = rel.attrib.get("Target", "")
            if rel_id and rel_target:
                rel_targets[rel_id] = _resolve_workbook_rel_target(rel_target)

    entries: list[tuple[str, str]] = []
    used: set[str] = set()
    for sheet_name, rel_id in workbook_sheets:
        member = rel_targets.get(rel_id)
        if (
            member
            and member in names
            and member not in used
            and _XLSX_WORKSHEET_RE.fullmatch(member)
        ):
            entries.append((sheet_name, member))
            used.add(member)
    if not entries:
        return [
            (workbook_sheets[i][0] if i < len(workbook_sheets) else Path(member).stem, member)
            for i, member in enumerate(numbered)
        ]
    for member in numbered:
        if member not in used:
            entries.append((Path(member).stem, member))
    return entries


def _extract_xlsx(target: Path, max_chars: int, max_items: int) -> dict[str, Any]:
    budget = [MAX_DECOMPRESSED_BYTES]
    with _open_ooxml(target) as zf:
        names = set(zf.namelist())
        metadata = _extract_core_metadata(zf, budget)
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            ss_root = _safe_xml_frombytes(_read_zip_member(zf, "xl/sharedStrings.xml", budget))
            for si in ss_root.findall(f"{_XML_NS_X}si"):
                # Per-string cap: no cell can ever expand past the caller's budget.
                shared_strings.append(
                    _join_capped(
                        (t.text for t in si.iter(f"{_XML_NS_X}t") if t.text), max_chars
                    )[0]
                )

        workbook_sheets: list[tuple[str, str]] = []
        if "xl/workbook.xml" in names:
            wb_root = _safe_xml_frombytes(_read_zip_member(zf, "xl/workbook.xml", budget))
            for sheet_el in wb_root.iter(f"{_XML_NS_X}sheet"):
                nm = sheet_el.attrib.get("name", "")
                if nm:
                    workbook_sheets.append(
                        (nm[:max_chars], sheet_el.attrib.get(f"{_XML_NS_R}id", ""))
                    )

        sheet_entries = _xlsx_sheet_entries(zf, names, budget, workbook_sheets)
        if not sheet_entries:
            raise DocumentExtractError("XLSX archive has no worksheets")

        sheets_out: list[dict[str, Any]] = []
        text_lines: list[str] = []
        truncated = False
        # Running character budget over the joined ``text``: row building stops
        # as soon as it is spent instead of truncating after the full walk.
        text_chars = 0
        budget_spent = False
        for s_name, sf in sheet_entries[:_XLSX_MAX_SHEETS]:
            if text_chars >= max_chars:
                budget_spent = True
                break
            s_root = _safe_xml_frombytes(_read_zip_member(zf, sf, budget))
            prefix = f"[{s_name}] "
            rows_out: list[list[str]] = []
            sheet_data = s_root.find(f"{_XML_NS_X}sheetData")
            if sheet_data is not None:
                for row_el in sheet_data.findall(f"{_XML_NS_X}row"):
                    if len(rows_out) >= max_items:
                        truncated = True
                        break
                    if text_chars >= max_chars:
                        budget_spent = True
                        break
                    # Upper bound on this row's line: newline + prefix + per-cell
                    # value and " | " separator.
                    row_cost = (1 if text_lines else 0) + len(prefix)
                    cells_by_idx: dict[int, str] = {}
                    for c_el in row_el.findall(f"{_XML_NS_X}c"):
                        c_ref = c_el.attrib.get("r", "")
                        c_type = c_el.attrib.get("t", "")
                        val = ""
                        if c_type == "inlineStr":
                            val = _join_capped(
                                (t.text for t in c_el.iter(f"{_XML_NS_X}t") if t.text),
                                max_chars,
                            )[0]
                        else:
                            v_el = c_el.find(f"{_XML_NS_X}v")
                            raw_v = v_el.text if (v_el is not None and v_el.text is not None) else ""
                            if c_type == "s" and raw_v.isdigit():
                                ss_i = int(raw_v)
                                val = shared_strings[ss_i] if 0 <= ss_i < len(shared_strings) else raw_v
                            elif c_type == "b":
                                val = "TRUE" if raw_v == "1" else "FALSE"
                            else:
                                val = raw_v
                        val = val[:max_chars]
                        col_i = _col_ref_to_index(c_ref) if c_ref else len(cells_by_idx)
                        cells_by_idx[col_i] = val
                        row_cost += len(val) + 3
                        if text_chars + row_cost > max_chars:
                            # Remaining cells of this row cannot fit the budget.
                            truncated = True
                            break
                    if cells_by_idx:
                        width = min(max(cells_by_idx) + 1, 100)
                        row_vals = [cells_by_idx.get(i, "") for i in range(width)]
                        rows_out.append(row_vals)
                        line = prefix + " | ".join(row_vals)
                        text_chars += len(line) + (1 if text_lines else 0)
                        text_lines.append(line)
            sheets_out.append({
                "name": s_name,
                "row_count": len(rows_out),
                "rows": rows_out[:50],
            })
            if budget_spent:
                break
        if budget_spent or len(sheet_entries) > _XLSX_MAX_SHEETS:
            truncated = True

    full_text = "\n".join(text_lines)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
        truncated = True
    return {
        "metadata": metadata,
        "sheet_count": len(sheet_entries),
        "sheets": sheets_out,
        "text": full_text,
        "truncated": truncated,
    }


def _slide_sort_key(member_name: str) -> tuple[int, str]:
    match = re.search(r"(\d+)\.xml$", member_name)
    return (int(match.group(1)) if match else 0, member_name)


def _extract_pptx(target: Path, max_chars: int, max_items: int) -> dict[str, Any]:
    budget = [MAX_DECOMPRESSED_BYTES]
    with _open_ooxml(target) as zf:
        names = set(zf.namelist())
        metadata = _extract_core_metadata(zf, budget)
        slide_files = sorted(
            (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=_slide_sort_key,
        )
        if not slide_files:
            raise DocumentExtractError("PPTX archive has no slides")

        slides_out: list[dict[str, Any]] = []
        text_lines: list[str] = []
        truncated = len(slide_files) > max_items
        # Running character budgets: slide text stops slide building once spent;
        # speaker notes have their own budget of the same size.
        text_chars = 0
        notes_chars = 0
        for idx, sf in enumerate(slide_files[:max_items], 1):
            if text_chars >= max_chars:
                truncated = True
                break
            s_root = _safe_xml_frombytes(_read_zip_member(zf, sf, budget))
            chunks: list[str] = []
            chunk_chars = 0
            for t in s_root.iter(f"{_XML_NS_A}t"):
                piece = t.text.strip() if t.text else ""
                if not piece:
                    continue
                room = max_chars - chunk_chars
                if room <= 0:
                    truncated = True
                    break
                if len(piece) > room:
                    piece = piece[:room]
                    truncated = True
                chunks.append(piece)
                chunk_chars += len(piece)
            notes_path = f"ppt/notesSlides/notesSlide{idx}.xml"
            notes = ""
            if notes_path in names:
                if notes_chars >= max_chars:
                    truncated = True
                else:
                    n_root = _safe_xml_frombytes(_read_zip_member(zf, notes_path, budget))
                    notes, cut = _join_capped(
                        (
                            t.text.strip()
                            for t in n_root.iter(f"{_XML_NS_A}t")
                            if t.text and t.text.strip()
                        ),
                        max_chars - notes_chars,
                        " ",
                    )
                    notes_chars += len(notes)
                    truncated = truncated or cut
            title = chunks[0] if chunks else ""
            body_text = (
                _join_capped(chunks[1:], max_chars, "\n")[0] if len(chunks) > 1 else ""
            )
            slides_out.append({
                "index": idx,
                "title": title,
                "text": body_text,
                "notes": notes,
            })
            line = f"Slide {idx}: " + " | ".join(chunks)
            text_chars += len(line) + (1 if text_lines else 0)
            text_lines.append(line)

    full_text = "\n".join(text_lines)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
        truncated = True
    return {
        "metadata": metadata,
        "slide_count": len(slide_files),
        "slides": slides_out[:100],
        "text": full_text,
        "truncated": truncated,
    }


class _HtmlCollector(HTMLParser):
    def __init__(self, max_items: int) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._in_heading = False
        self._heading_buf: list[str] = []
        self._title_buf: list[str] = []
        self.headings: list[str] = []
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self._max_items = max_items

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in ("script", "style", "noscript"):
            self._skip_depth += 1
        elif lower == "title":
            self._in_title = True
        elif lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = True
            self._heading_buf = []
        elif lower == "a" and len(self.links) < self._max_items:
            for k, v in attrs:
                if k.lower() == "href" and v and v.strip():
                    self.links.append(v.strip())
                    break

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif lower == "title":
            self._in_title = False
        elif lower in ("h1", "h2", "h3", "h4", "h5", "h6") and self._in_heading:
            self._in_heading = False
            heading = " ".join("".join(self._heading_buf).split())
            if heading and len(self.headings) < self._max_items:
                self.headings.append(heading)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_buf.append(data)
        if self._in_heading:
            self._heading_buf.append(data)
        cleaned = " ".join(data.split())
        if cleaned:
            self.text_parts.append(cleaned)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_buf).split())


def _extract_html(target: Path, max_chars: int, max_items: int) -> dict[str, Any]:
    raw = target.read_bytes()
    parser = _HtmlCollector(max_items)
    parser.feed(raw.decode("utf-8", errors="replace"))
    full_text = "\n".join(parser.text_parts)
    truncated = len(full_text) > max_chars
    if truncated:
        full_text = full_text[:max_chars]
    return {
        "title": parser.title,
        "heading_count": len(parser.headings),
        "headings": parser.headings[:50],
        "link_count": len(parser.links),
        "links": parser.links[:50],
        "text": full_text,
        "truncated": truncated,
    }


def _extract_svg(target: Path, max_chars: int, max_items: int) -> dict[str, Any]:
    raw = target.read_bytes()
    root = _safe_xml_frombytes(raw)
    tag_counts: dict[str, int] = {}
    labels: list[str] = []
    for elem in root.iter():
        local_tag = elem.tag.rsplit("}", 1)[-1]
        tag_counts[local_tag] = tag_counts.get(local_tag, 0) + 1
        if local_tag in ("text", "tspan", "title", "desc") and elem.text and elem.text.strip():
            if len(labels) < max_items:
                labels.append(elem.text.strip())
    full_text = "\n".join(labels)
    truncated = len(full_text) > max_chars
    if truncated:
        full_text = full_text[:max_chars]
    return {
        "width": root.attrib.get("width", ""),
        "height": root.attrib.get("height", ""),
        "viewBox": root.attrib.get("viewBox", ""),
        "element_counts": dict(sorted(tag_counts.items())),
        "text_label_count": len(labels),
        "text": full_text,
        "truncated": truncated,
    }


def _extract_subtitles(target: Path, fmt: str, max_chars: int, max_items: int) -> dict[str, Any]:
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    time_re = _SRT_TIME_RE if fmt == "srt" else _VTT_TIME_RE
    cues: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        m = time_re.match(lines[idx])
        if not m:
            idx += 1
            continue
        start_ts, end_ts = m.group(1), m.group(2)
        idx += 1
        cue_lines: list[str] = []
        while idx < len(lines) and lines[idx].strip():
            cue_lines.append(lines[idx].strip())
            idx += 1
        cues.append({
            "start": start_ts,
            "end": end_ts,
            "text": " ".join(cue_lines),
        })
    truncated = len(cues) > max_items
    shown = cues[:max_items]
    full_text = "\n".join(c["text"] for c in shown if c["text"])
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
        truncated = True
    return {
        "cue_count": len(cues),
        "first_timestamp": cues[0]["start"] if cues else "",
        "last_timestamp": cues[-1]["end"] if cues else "",
        "cues": shown[:50],
        "text": full_text,
        "truncated": truncated,
    }


def extract_document(
    path: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_items: int = DEFAULT_MAX_ITEMS,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict[str, Any]:
    """Extract structured text, tables, slides, or cues from a guarded document."""
    target = _resolve_target(
        path,
        extra_roots=extra_roots,
        bypass=bypass,
        developer_authorized=developer_authorized,
    )
    fmt = _SUFFIX_MAP.get(target.suffix.lower())
    if not fmt:
        raise DocumentExtractError(
            "unsupported document extension %r (supported: %s)"
            % (target.suffix, ", ".join(sorted(_SUFFIX_MAP)))
        )
    chars_cap = _bounded_int(max_chars, DEFAULT_MAX_CHARS, 256, HARD_MAX_CHARS)
    items_cap = _bounded_int(max_items, DEFAULT_MAX_ITEMS, 1, HARD_MAX_ITEMS)
    raw_bytes = target.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    if fmt == "docx":
        payload = _extract_docx(target, chars_cap, items_cap)
    elif fmt == "xlsx":
        payload = _extract_xlsx(target, chars_cap, items_cap)
    elif fmt == "pptx":
        payload = _extract_pptx(target, chars_cap, items_cap)
    elif fmt == "html":
        payload = _extract_html(target, chars_cap, items_cap)
    elif fmt == "svg":
        payload = _extract_svg(target, chars_cap, items_cap)
    else:
        payload = _extract_subtitles(target, fmt, chars_cap, items_cap)

    return {
        "ok": True,
        "path": str(target),
        "format": fmt,
        "bytes": len(raw_bytes),
        "sha256": sha256,
        **payload,
    }


def format_result(data: dict[str, Any]) -> str:
    lines = [
        f"document extraction ({data.get('format', 'unknown')}): {data.get('path', '')}",
        f"  bytes: {data.get('bytes', 0)}  sha256: {data.get('sha256', '')}",
    ]
    for key in (
        "title", "paragraph_count", "heading_count", "table_count",
        "sheet_count", "slide_count", "cue_count", "link_count",
        "text_label_count", "width", "height", "viewBox",
        "first_timestamp", "last_timestamp",
    ):
        if key in data and data[key] not in ("", None):
            lines.append(f"  {key}: {data[key]}")
    meta = data.get("metadata") or {}
    if meta:
        lines.append("  metadata: " + ", ".join(f"{k}={v}" for k, v in sorted(meta.items())))
    if data.get("truncated"):
        lines.append("  truncated: true")
    text = str(data.get("text") or "").strip()
    if text:
        lines.append("  --- extracted text ---")
        lines.append(text[:4000])
    return "\n".join(lines)


__all__ = [
    "DocumentExtractError",
    "SUPPORTED_FORMATS",
    "extract_document",
    "format_result",
]
