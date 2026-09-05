"""Minimal XLSX export for hydrograph tables.

The implementation writes Office Open XML directly with the Python standard
library. This keeps the runtime lightweight while producing a real .xlsx file
that opens in Excel/LibreOffice without adding a spreadsheet dependency.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_mime_type() -> str:
    return _XLSX_MIME


def _inline_cell(ref: str, value: Any, style: int = 0) -> str:
    text = escape(str(value if value is not None else ""))
    style_attr = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t>{text}</t></is></c>'


def _number_cell(ref: str, value: Any, style: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _inline_cell(ref, "", style)
    if not math.isfinite(number):
        return _inline_cell(ref, "", style)
    style_attr = f' s="{style}"' if style else ""
    return f'<c r="{ref}"{style_attr}><v>{repr(number)}</v></c>'


def _interval_minutes(interval: Any) -> int:
    match = re.search(r"(\d+)\s*Minute", str(interval or ""), re.I)
    return max(1, int(match.group(1))) if match else 5


def _elapsed_excel_time(index: int, interval: Any) -> float:
    """Excel stores elapsed time as a fraction of one day (builtin format 20)."""
    total_minutes = max(0, int(index)) * _interval_minutes(interval)
    return total_minutes / 1_440.0


def _scenario_title(return_period_years: Any, scenario_label: str | None, duration_hours: Any = None) -> str:
    try:
        years = int(float(return_period_years))
    except (TypeError, ValueError):
        years = 0
    if years > 0:
        title = f"Debit Banjir Kala Ulang {years} Tahun"
    else:
        label = str(scenario_label or "").strip()
        title = f"Debit Banjir {label}" if label else "Debit Banjir"
    try:
        duration = int(duration_hours)
    except (TypeError, ValueError):
        duration = 0
    return f"{title} — Hujan {duration} Jam" if duration > 0 else title


def hydrograph_filename(
    labels: list[str],
    *,
    return_period_years: Any = None,
    scenario_label: str | None = None,
    duration_hours: Any = None,
) -> str:
    first = str(labels[0] if labels else "Titik Kontrol").strip() or "Titik Kontrol"
    first = first.replace(" - ", " – ")
    first = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", first)
    first = re.sub(r"\s+", " ", first).strip(" .")[:90] or "Titik Kontrol"
    suffix = " dkk" if len(labels) > 1 else ""
    try:
        years = int(float(return_period_years))
    except (TypeError, ValueError):
        years = 0
    scenario_text = f"Kala Ulang {years} Tahun" if years > 0 else str(scenario_label or "").strip()
    try:
        duration = int(duration_hours)
    except (TypeError, ValueError):
        duration = 0
    duration_text = f" Hujan {duration} Jam" if duration > 0 else ""
    prefix = f"Debit Banjir {scenario_text}{duration_text}".strip()
    return f"{prefix} {first}{suffix}.xlsx"


def _safe_sheet_name(raw: Any, index: int, used: set[str]) -> str:
    name = str(raw or f"Titik {index + 1}").strip() or f"Titik {index + 1}"
    name = re.sub(r"[\[\]:*?/\\]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" '\t\r\n")[:31] or f"Titik {index + 1}"
    base = name
    suffix_index = 2
    while name.casefold() in used:
        suffix = f" ({suffix_index})"
        name = f"{base[:max(1, 31 - len(suffix))]}{suffix}"
        suffix_index += 1
    used.add(name.casefold())
    return name


def _coordinate_text(point: dict[str, Any]) -> str:
    """Coordinates in the export identify the snapped control point."""
    lat = point.get("snapped_lat", point.get("input_lat", point.get("lat")))
    lon = point.get("snapped_lon", point.get("input_lon", point.get("lon")))
    try:
        return f"{float(lat):.6f}, {float(lon):.6f}"
    except (TypeError, ValueError):
        return "—"


def _sheet_xml(point: dict[str, Any], *, title: str, interval: Any, label: str) -> str:
    series = list(point.get("series") or [])
    last_row = max(2, len(series) + 1)
    full_title = f"{title} {label}".strip()
    rows: list[str] = [
        f'<row r="1">'
        f'{_inline_cell("A1", "Jam ke-", 1)}'
        f'{_inline_cell("B1", "Debit (m3/det)", 1)}'
        f'{_inline_cell("D1", full_title, 3)}'
        f'</row>',
        f'<row r="2">'
        f'{_number_cell("A2", _elapsed_excel_time(0, interval), 4)}'
        f'{_number_cell("B2", series[0] if series else None, 2)}'
        f'{_inline_cell("D2", _coordinate_text(point), 3)}'
        f'</row>',
    ]
    for idx, value in enumerate(series[1:], 1):
        row_no = idx + 2
        rows.append(
            f'<row r="{row_no}">'
            f'{_number_cell(f"A{row_no}", _elapsed_excel_time(idx, interval), 4)}'
            f'{_number_cell(f"B{row_no}", value, 2)}'
            f'</row>'
        )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:D{last_row}"/>
  <cols>
    <col min="1" max="1" width="8.7109375" style="2" customWidth="1"/>
    <col min="2" max="2" width="15" style="2" customWidth="1"/>
    <col min="3" max="16384" width="9.140625" style="2" customWidth="1"/>
  </cols>
  <sheetData>{''.join(rows)}</sheetData>
</worksheet>'''


def build_hydrograph_xlsx(
    payload: dict[str, Any],
    *,
    return_period_years: Any = None,
    scenario_label: str | None = None,
    duration_hours: Any = None,
    sheet_names: list[str] | None = None,
) -> bytes:
    points = [item for item in (payload.get("points") or []) if isinstance(item, dict)]
    interval = payload.get("interval") or "5Minute"
    labels = [
        str(item.get("label") or item.get("point_id") or f"Titik {idx + 1}").replace(" - ", " – ")
        for idx, item in enumerate(points)
    ]
    title = _scenario_title(return_period_years, scenario_label, duration_hours)

    used_names: set[str] = set()
    names: list[str] = []
    for idx, point in enumerate(points):
        candidate = sheet_names[idx] if sheet_names and idx < len(sheet_names) else point.get("sheet_name")
        if not candidate:
            full_label = labels[idx]
            candidate = full_label.split(" – ", 1)[-1].strip() if " – " in full_label else full_label
        names.append(_safe_sheet_name(candidate, idx, used_names))

    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="20" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    sheet_nodes = ''.join(
        f'<sheet name="{escape(name)}" sheetId="{idx + 1}" r:id="rId{idx + 1}"/>'
        for idx, name in enumerate(names)
    )
    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{sheet_nodes}</sheets>
</workbook>'''

    rel_nodes = ''.join(
        f'<Relationship Id="rId{idx + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx + 1}.xml"/>'
        for idx in range(len(points))
    )
    rel_nodes += f'<Relationship Id="rId{len(points) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    workbook_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rel_nodes}</Relationships>'''

    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>'''

    sheet_overrides = ''.join(
        f'<Override PartName="/xl/worksheets/sheet{idx + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for idx in range(len(points))
    )
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  {sheet_overrides}
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>'''

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(title)}</dc:title><dc:creator>Penelusuran Banjir</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
</cp:coreProperties>'''

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        for idx, point in enumerate(points):
            archive.writestr(
                f"xl/worksheets/sheet{idx + 1}.xml",
                _sheet_xml(point, title=title, interval=interval, label=labels[idx]),
            )
    return buffer.getvalue()
