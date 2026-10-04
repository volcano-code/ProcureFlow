"""Disposable stdlib-only CSV/XLSX decoder; invoked by tabular.parse_table.

This is a resource-bounded subprocess with Python audit guards, NOT a complete
OS/kernel sandbox. It never evaluates document text, formulas, or macros. Deploy
untrusted uploads in a hardened container as an additional production boundary.
The worker receives raw bytes on stdin and emits bounded JSON on stdout.
"""
from __future__ import annotations

import csv
import encodings.utf_8_sig
import encodings.cp437
import encodings.latin_1
import encodings.utf_16_be
import encodings.utf_16_le
import encodings.cp1252
import io
import json
import os
import re
import sys
import zipfile
import zlib  # Load decompression before installing the no-file-access guard.
from xml.etree import ElementTree as ET

MAX_INPUT = 2 * 1024 * 1024
MAX_EXPANDED = 5_000_000
MAX_OUTPUT = 6_000_000
MAX_ROWS = 1000
MAX_COLUMNS = 64
MAX_CELLS = 20_000
MAX_TEXT = 150_000
MAX_CELL_TEXT = 4096
MAX_SHEETS = 10
N = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class ParseError(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def restrict_worker():
    """Fail closed without POSIX limits; audit hooks are defense-in-depth only."""
    try:
        import resource
        for key, amount in ((resource.RLIMIT_AS, 256 * 1024 * 1024),
                            (resource.RLIMIT_CPU, 2), (resource.RLIMIT_NOFILE, 32),
                            (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(key, (amount, amount))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ImportError, OSError, ValueError) as error:
        raise ParseError("PARSER_ISOLATION_UNAVAILABLE", "Required parser resource limits are unavailable", 503) from error

    def guard(event, args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
            raise PermissionError("Parser worker operation denied")
        if event in {"os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty",
                     "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.link", "os.symlink",
                     "os.truncate", "os.chmod", "os.chown", "os.putenv", "os.unsetenv"}:
            raise PermissionError("Parser worker operation denied")
    sys.addaudithook(guard)


def column_name(number):
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def column_number(name):
    result = 0
    for char in name:
        result = result * 26 + ord(char) - 64
    return result


def validate_xml_encoding(data):
    # Closed UTF-8 encoding support prevents entity/declaration scans from being
    # bypassed with UTF-16, UTF-7, or an incompatible XML encoding declaration.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ParseError("UNSAFE_XML", "Only UTF-8 XML is accepted") from error
    declaration = re.match(r"\s*<\?xml\s+([^?]*)\?>", text, re.IGNORECASE)
    encoding = re.search(r"encoding\s*=\s*(['\"])([^'\"]+)\1", declaration[1], re.IGNORECASE) if declaration else None
    if encoding and encoding[2].lower() not in {"utf-8", "utf8"}:
        raise ParseError("UNSAFE_XML", "Only UTF-8 XML encoding declarations are accepted")
    if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ParseError("UNSAFE_XML", "DTD/entity declarations and non-UTF-8 XML are not accepted")


def xml(data):
    validate_xml_encoding(data)
    return ET.fromstring(data)


def formula_like(value):
    text = value.lstrip()
    return bool(text and (text[0] in "=@" or
                (text[0] in "+-" and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text))))


class Budget:
    def __init__(self):
        self.cells = self.text = 0

    def cell(self, column, row, value, formula=False, **extra):
        self.cells += 1
        self.text += len(value)
        if self.cells > MAX_CELLS or self.text > MAX_TEXT or len(value) > MAX_CELL_TEXT:
            raise ParseError("TABLE_LIMIT", "Table cell count or extracted text limit exceeded", 413)
        return {"column": column, "cell": f"{column}{row}", "value": value, "formula": formula, **extra}


def csv_table(data):
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ParseError("BINARY_TEXT", "Binary content is not accepted as CSV")
    budget, rows, width = Budget(), [], 0
    csv.field_size_limit(MAX_CELL_TEXT)
    # Support common UTF-8 comma/semicolon/tab exports, without guessing encodings.
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    previous_line = 0
    for number, values in enumerate(reader, 1):
        if number > MAX_ROWS or len(values) > MAX_COLUMNS:
            raise ParseError("TABLE_LIMIT", "Table row or column limit exceeded", 413)
        start_line, previous_line = previous_line + 1, reader.line_num
        width = max(width, len(values))
        cells = [budget.cell(column_name(index), number, value, formula_like(value))
                 for index, value in enumerate(values, 1)]
        rows.append({"row": number, "line_start": start_line, "line_end": reader.line_num, "cells": cells})
    return [{"name": "CSV", "rows": rows, "row_count": len(rows), "column_count": width,
             "state": "visible", "merged_ranges": []}]


def xlsx_table(data):
    if not data.startswith(b"PK"):
        raise ParseError("FILE_SIGNATURE", "This file is not an XLSX archive")
    budget, sheets = Budget(), []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 250 or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ParseError("ARCHIVE_LIMIT", "XLSX expanded size or entry limit exceeded", 413)
        names, files, expanded = set(), {}, 0
        for entry in entries:
            name = entry.filename
            lowered = name.lower()
            if name in names or "\\" in name or name.startswith("/") or any(p in {".", ".."} for p in name.split("/")):
                raise ParseError("UNSAFE_ARCHIVE_PATH", "Duplicate or unsafe XLSX archive path")
            names.add(name)
            if entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ParseError("ACTIVE_CONTENT", "Encrypted archive entries and symlinks are not accepted")
            if any(part in lowered for part in ("externallinks/", "vbaproject", "macrosheets/", "embeddings/", "activex/", "connections.xml")):
                raise ParseError("ACTIVE_CONTENT", "Macros, embedded objects and external workbook links are not accepted")
            if entry.file_size / max(entry.compress_size, 1) > 150:
                raise ParseError("ARCHIVE_LIMIT", "Suspicious XLSX compression ratio", 413)
            with archive.open(entry) as stream:
                blob = stream.read(MAX_EXPANDED - expanded + 1)
            expanded += len(blob)
            if expanded > MAX_EXPANDED:
                raise ParseError("ARCHIVE_LIMIT", "XLSX expanded size limit exceeded", 413)
            files[name] = blob
        # Reject external relationships anywhere, not just workbook.xml.rels.
        for name, blob in files.items():
            if name.lower().endswith(".rels"):
                for relation in xml(blob):
                    if relation.attrib.get("TargetMode", "").lower() == "external":
                        raise ParseError("ACTIVE_CONTENT", "External workbook relationships are not accepted")
                    if "macrosheet" in relation.attrib.get("Type", "").lower():
                        raise ParseError("ACTIVE_CONTENT", "Excel macro sheet relationships are not accepted")
            if name.lower().endswith(".xml"):
                # Validate even XML parts that extraction does not consume.
                validate_xml_encoding(blob)
        types = files.get("[Content_Types].xml", b"").lower()
        if any(token in types for token in (b"macroenabled", b"vbaproject", b"macrosheet")):
            raise ParseError("ACTIVE_CONTENT", "Macro-enabled content is not accepted")
        shared = []
        if "xl/sharedStrings.xml" in files:
            for item in xml(files["xl/sharedStrings.xml"]).findall("m:si", N):
                value = "".join(t.text or "" for t in item.findall(".//m:t", N))
                if len(value) > MAX_CELL_TEXT:
                    raise ParseError("TABLE_LIMIT", "Shared string exceeds cell limit", 413)
                shared.append(value)
                if len(shared) > MAX_CELLS:
                    raise ParseError("TABLE_LIMIT", "Too many shared strings", 413)
        book = xml(files["xl/workbook.xml"])
        relationships = xml(files["xl/_rels/workbook.xml.rels"])
        targets, target_types = {}, {}
        for relation in relationships:
            target = relation.attrib.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if "\\" in path or any(p in {".", ".."} for p in path.split("/")) or ":" in path:
                raise ParseError("UNSAFE_ARCHIVE_PATH", "Unsafe workbook relationship path")
            rid = relation.attrib["Id"]
            if rid in targets:
                raise ParseError("PARSE_FAILED", "Duplicate workbook relationship")
            targets[rid] = path
            target_types[rid] = relation.attrib.get("Type", "")
        book_sheets = book.findall("m:sheets/m:sheet", N)
        if len(book_sheets) > MAX_SHEETS:
            raise ParseError("TABLE_LIMIT", "At most ten sheets are supported", 413)
        sheet_names = set()
        for sheet in book_sheets:
            name = sheet.attrib["name"]
            if not name or len(name) > 100 or name in sheet_names:
                raise ParseError("PARSE_FAILED", "Invalid or duplicate sheet name")
            sheet_names.add(name)
            if target_types[sheet.attrib[R]] != "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet":
                raise ParseError("ACTIVE_CONTENT", "Only ordinary worksheet relationships are accepted")
            root = xml(files[targets[sheet.attrib[R]]])
            if root.tag != "{" + N["m"] + "}worksheet":
                raise ParseError("ACTIVE_CONTENT", "Only ordinary worksheet XML is accepted")
            rows, seen_rows, width = [], set(), 0
            for row in root.findall("m:sheetData/m:row", N):
                number = int(row.attrib["r"])
                if number < 1 or number > MAX_ROWS:
                    raise ParseError("TABLE_LIMIT", "Sheet row address exceeds supported limit", 413)
                if number in seen_rows:
                    raise ParseError("PARSE_FAILED", "Duplicate sheet row address")
                seen_rows.add(number)
                cells, seen_cells = [], set()
                for cell in row.findall("m:c", N):
                    reference = cell.attrib.get("r", "")
                    match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", reference)
                    if not match or int(match[2]) != number or reference in seen_cells:
                        raise ParseError("PARSE_FAILED", "Invalid or duplicate spreadsheet cell address")
                    column = column_number(match[1])
                    if column > MAX_COLUMNS:
                        raise ParseError("TABLE_LIMIT", "Sheet column address exceeds supported limit", 413)
                    seen_cells.add(reference)
                    width = max(width, column)
                    value_node, formula_node = cell.find("m:v", N), cell.find("m:f", N)
                    value = value_node.text or "" if value_node is not None else ""
                    formula = formula_node is not None
                    # Array/shared/data-table formulas can populate cached values
                    # in cells without their own <f>. Reject ranges rather than
                    # accidentally trust a spill cell as an independent quote.
                    if (formula and formula_node.attrib.get("ref", reference) != reference) or "cm" in cell.attrib or "vm" in cell.attrib:
                        raise ParseError("UNSUPPORTED_FORMULA_RANGE", "Formula ranges and metadata-backed cells require a values-only export")
                    if formula:
                        value = "=" + (formula_node.text or "[SHARED_FORMULA]")
                    elif cell.attrib.get("t") == "s":
                        index = int(value)
                        if index < 0 or index >= len(shared):
                            raise ParseError("PARSE_FAILED", "Invalid shared string reference")
                        value = shared[index]
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//m:t", N))
                    cells.append(budget.cell(match[1], number, value, formula,
                                             error=cell.attrib.get("t") == "e"))
                cells.sort(key=lambda c: column_number(c["column"]))
                rows.append({"row": number, "cells": cells, "hidden": row.attrib.get("hidden") == "1"})
            rows.sort(key=lambda r: r["row"])
            merged = [merge.attrib["ref"] for merge in root.findall("m:mergeCells/m:mergeCell", N)]
            if len(merged) > 1000:
                raise ParseError("TABLE_LIMIT", "Too many merged ranges", 413)
            for reference in merged:
                if not re.fullmatch(r"[A-Z]{1,2}[1-9]\d{0,3}:[A-Z]{1,2}[1-9]\d{0,3}", reference):
                    raise ParseError("PARSE_FAILED", "Invalid merged range")
            sheets.append({"name": name, "rows": rows, "row_count": max(seen_rows, default=0),
                           "column_count": width, "state": sheet.attrib.get("state", "visible"),
                           "merged_ranges": merged})
    return sheets


def main():
    try:
        # Trusted application imports happen before filesystem access is denied.
        # The isolated interpreter never imports from an uploaded archive.
        document_parser = None
        if sys.argv[1] == "document":
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import pypdf  # Preload its lazy PDF import before denying file opens.
            from procureflow.parsers import parse_document as document_parser
            from procureflow.errors import DomainError
        restrict_worker()
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT:
            raise ParseError("FILE_TOO_LARGE", "File exceeds the 2 MiB import limit", 413)
        kind = sys.argv[1]
        if kind == "document":
            # The compatibility route must not bypass the table route's archive,
            # XML, external-link or formula-range validation.
            if sys.argv[2].lower().endswith(".xlsx"):
                xlsx_table(data)
            try:
                document = document_parser(sys.argv[2], data, sys.argv[3])
            except DomainError as error:
                raise ParseError(error.code, error.message, error.status_code) from error
            result = {"ok": True, "document": document}
        elif kind not in {"csv", "xlsx"}:
            raise ParseError("UNSUPPORTED_FILE", "Only CSV and XLSX tables are supported", 415)
        else:
            sheets = csv_table(data) if kind == "csv" else xlsx_table(data)
            if not sheets or not any(any(c["value"].strip() for r in s["rows"] for c in r["cells"]) for s in sheets):
                raise ParseError("EMPTY_TABLE", "No nonempty table cells were found")
            result = {"ok": True, "table": {"kind": kind, "sheets": sheets}}
    except ParseError as error:
        result = {"ok": False, "error": {"code": error.code, "message": error.message, "status": error.status}}
    except MemoryError:
        result = {"ok": False, "error": {"code": "PARSER_LIMIT", "message": "Parser memory limit exceeded", "status": 413}}
    except Exception:
        result = {"ok": False, "error": {"code": "PARSE_FAILED", "message": "File format could not be safely parsed", "status": 422}}
    payload = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_OUTPUT:
        payload = b'{"ok":false,"error":{"code":"TABLE_LIMIT","message":"Parser output limit exceeded","status":413}}'
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main()
