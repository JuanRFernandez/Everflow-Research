"""Low-level xlsx package surgery.

Used for exactly one job: putting back the cached formula results that openpyxl
discards on rewrite.

Why this is safe here, and would not be in general: in this workbook only DASHBOARD
references PARTNERS, and only columns C, G, Y, Z and AI -- none of which the
enricher writes. RESORTS_SBI is entirely self-contained. So no write this tool makes
can change any formula's result, and the pre-existing cached results remain correct.
`writer.assert_no_precedents_touched` enforces that premise before reinjection runs.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_CELL_RE = re.compile(r"<c\b([^>]*?)(/>|>((?:(?!</c>).)*?)</c>)", re.S)
_REF_RE = re.compile(r'r="([A-Z]+\d+)"')
_TYPE_ATTR_RE = re.compile(r'\st="[^"]*"')
_VALUE_RE = re.compile(r"<v>(.*?)</v>", re.S)
_EMPTY_VALUE_RE = re.compile(r"<v\s*/>|<v></v>")


def sheet_part_map(path: Path) -> dict[str, str]:
    """Map sheet title -> worksheet part name, e.g. {'PARTNERS': 'xl/worksheets/sheet3.xml'}.

    Resolved through the relationship table rather than assumed from ordering, so a
    workbook whose parts are numbered unusually still maps correctly.
    """
    with zipfile.ZipFile(path) as zf:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))

    targets: dict[str, str] = {}
    for rel in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        target = rel.attrib.get("Target", "")
        target = target[1:] if target.startswith("/") else target
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("./")
        targets[rel.attrib["Id"]] = target

    out: dict[str, str] = {}
    sheets = wb_root.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        return out
    for sheet in sheets.findall(f"{{{_MAIN_NS}}}sheet"):
        rid = sheet.attrib.get(f"{{{_REL_NS}}}id")
        if rid and rid in targets:
            out[sheet.attrib["name"]] = targets[rid]
    return out


def read_cached_values(path: Path) -> dict[tuple[str, str], str | None]:
    """Cached formula results keyed by (sheet title, cell ref).

    `<v></v>` -- what openpyxl emits -- reads back as `None`, the same as a missing
    element, because both mean "no usable cached result".
    """
    parts = sheet_part_map(path)
    out: dict[tuple[str, str], str | None] = {}
    with zipfile.ZipFile(path) as zf:
        for title, part in parts.items():
            try:
                xml = zf.read(part).decode("utf-8")
            except KeyError:  # pragma: no cover - malformed package
                continue
            for match in _CELL_RE.finditer(xml):
                attrs, body = match.group(1), match.group(3) or ""
                if "<f" not in body:
                    continue
                ref = _REF_RE.search(attrs)
                if not ref:
                    continue
                value = _VALUE_RE.search(body)
                out[(title, ref.group(1))] = (
                    value.group(1) if value and value.group(1) != "" else None
                )
    return out


def read_cached_types(path: Path) -> dict[tuple[str, str], str | None]:
    """The `t=` attribute of every formula cell, keyed by (sheet title, cell ref)."""
    parts = sheet_part_map(path)
    out: dict[tuple[str, str], str | None] = {}
    with zipfile.ZipFile(path) as zf:
        for title, part in parts.items():
            try:
                xml = zf.read(part).decode("utf-8")
            except KeyError:  # pragma: no cover
                continue
            for match in _CELL_RE.finditer(xml):
                attrs, body = match.group(1), match.group(3) or ""
                if "<f" not in body:
                    continue
                ref = _REF_RE.search(attrs)
                if not ref:
                    continue
                type_match = re.search(r'\st="([^"]*)"', attrs)
                out[(title, ref.group(1))] = type_match.group(1) if type_match else None
    return out


def reinject_cached_values(target: Path, source: Path) -> int:
    """Copy every formula's cached result from `source` into `target`, in place.

    Only formula cells that exist in both files and hold a cached result in the
    source are touched. Formula text in `target` is never modified.

    Returns:
        The number of cells repaired.
    """
    cached = read_cached_values(source)
    types = read_cached_types(source)
    if not cached:
        return 0

    target_parts = sheet_part_map(target)
    repaired = 0

    with zipfile.ZipFile(target) as zf:
        contents = {name: zf.read(name) for name in zf.namelist()}
        order = list(zf.namelist())

    for title, part in target_parts.items():
        wanted = {ref: v for (t, ref), v in cached.items() if t == title and v is not None}
        if not wanted or part not in contents:
            continue
        xml = contents[part].decode("utf-8")

        def _fix(match: re.Match[str], *, wanted: dict[str, str] = wanted,
                 title: str = title) -> str:
            nonlocal repaired
            attrs, closing, body = match.group(1), match.group(2), match.group(3) or ""
            if closing == "/>" or "<f" not in body:
                return match.group(0)
            ref_match = _REF_RE.search(attrs)
            if not ref_match:
                return match.group(0)
            value = wanted.get(ref_match.group(1))
            if value is None:
                return match.group(0)

            new_body, count = _EMPTY_VALUE_RE.subn(f"<v>{value}</v>", body, count=1)
            if count == 0:
                if _VALUE_RE.search(body):
                    return match.group(0)  # already carries a cached result
                new_body = body + f"<v>{value}</v>"

            source_type = types.get((title, ref_match.group(1)))
            new_attrs = _TYPE_ATTR_RE.sub("", attrs)
            if source_type:
                new_attrs = f'{new_attrs.rstrip()} t="{source_type}"'
            repaired += 1
            return f"<c{new_attrs}>{new_body}</c>"

        contents[part] = _CELL_RE.sub(_fix, xml).encode("utf-8")

    tmp = target.with_suffix(target.suffix + ".reinject.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in order:
            zf.writestr(name, contents[name])
    shutil.move(str(tmp), str(target))
    return repaired
