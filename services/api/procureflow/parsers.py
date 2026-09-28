"""Conservative key/value extraction, not a general document-understanding model.

Supported: UTF-8 key:value TXT, two-column key/value CSV, text PDF, and key/value
XLSX. Unknown or conflicting fields remain unknown. Uploaded text never executes.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from pydantic import ValidationError
from .contracts import QuoteValues
from .errors import DomainError

ALIASES = {
    "供应商编码": "supplier_id", "型号": "sku", "数量": "quantity", "单位": "uom",
    "单价": "unit_price", "税价模式": "tax_mode", "税率": "tax_rate", "运费": "shipping_cost",
    "折扣": "discount", "交期天数": "delivery_days", "币种": "currency",
}
KNOWN = set(QuoteValues.model_fields)
N = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise DomainError("UNSAFE_XML", "DTD/entity declarations are not accepted", 422)
    return ET.fromstring(data)


def _xlsx_fragments(data: bytes) -> list[dict]:
    fragments = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.infolist()
        if len(info) > 250 or sum(x.file_size for x in info) > 5_000_000:
            raise DomainError("ARCHIVE_LIMIT", "XLSX expanded size or entry limit exceeded", 413)
        for entry in info:
            if entry.file_size / max(entry.compress_size, 1) > 150:
                raise DomainError("ARCHIVE_LIMIT", "Suspicious XLSX compression ratio", 413)
            if "externallinks/" in entry.filename.lower() or entry.filename.endswith("vbaProject.bin"):
                raise DomainError("ACTIVE_CONTENT", "External workbook links and macros are not accepted", 422)
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in _xml(z.read("xl/sharedStrings.xml")).findall("m:si", N):
                shared.append("".join(t.text or "" for t in si.findall(".//m:t", N)))
        book = _xml(z.read("xl/workbook.xml"))
        rels = _xml(z.read("xl/_rels/workbook.xml.rels"))
        targets = {}
        for rel in rels:
            if rel.attrib.get("TargetMode") == "External":
                raise DomainError("ACTIVE_CONTENT", "External workbook relationships are not accepted", 422)
            target = rel.attrib.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in path.split("/"):
                raise DomainError("UNSAFE_ARCHIVE_PATH", "Invalid workbook relationship", 422)
            targets[rel.attrib["Id"]] = path
        for sheet in book.findall("m:sheets/m:sheet", N):
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            root = _xml(z.read(targets[rid]))
            for row in root.findall("m:sheetData/m:row", N):
                cells = {}
                for cell in row.findall("m:c", N):
                    ref = cell.attrib.get("r", "")
                    col = re.sub(r"\d", "", ref)
                    value = cell.find("m:v", N)
                    text = value.text if value is not None and value.text else ""
                    if cell.find("m:f", N) is not None:
                        text = "[FORMULA_NOT_EVALUATED]"
                    elif cell.attrib.get("t") == "s":
                        text = shared[int(text)]
                    elif cell.attrib.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in cell.findall(".//m:t", N))
                    cells[col] = (ref, text)
                if "A" in cells and "B" in cells:
                    key, value = cells["A"][1], cells["B"][1]
                    fragments.append({"text": f"{key}: {value}", "locator": {
                        "kind": "xlsx", "sheet": sheet.attrib["name"], "cell_range": cells["B"][0],
                        "label_cell": cells["A"][0],
                    }})
    return fragments


def fragments_from_file(filename: str, data: bytes) -> list[dict]:
    extension = Path(filename).suffix.lower()
    if extension not in {".txt", ".csv", ".pdf", ".xlsx"}:
        raise DomainError("UNSUPPORTED_FILE", "Only .txt, .csv, text .pdf and key/value .xlsx are supported", 415)
    try:
        if extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise DomainError("FILE_SIGNATURE", "This file is not a PDF", 422)
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise DomainError("ENCRYPTED_PDF", "Encrypted PDFs are not supported", 422)
            if len(reader.pages) > 10:
                raise DomainError("PAGE_LIMIT", "At most ten pages per local-alpha document", 413)
            fragments = []
            for page_no, page in enumerate(reader.pages, start=1):
                content = page.get_contents()
                if content and len(content.get_data()) > 5_000_000:
                    raise DomainError("PDF_STREAM_LIMIT", "PDF content stream is too large", 413)
                text = page.extract_text() or ""
                if len(text) > 100_000:
                    raise DomainError("TEXT_LIMIT", "PDF extracted text is too large", 413)
                fragments.extend({"text": line, "locator": {"kind": "pdf", "page": page_no, "line": n}}
                                 for n, line in enumerate(text.splitlines(), 1) if line.strip())
            if not fragments:
                raise DomainError("SCANNED_PDF_UNSUPPORTED", "No text layer found; OCR is not enabled", 422)
        elif extension == ".xlsx":
            if not data.startswith(b"PK"):
                raise DomainError("FILE_SIGNATURE", "This file is not an XLSX archive", 422)
            fragments = _xlsx_fragments(data)
        else:
            text = data.decode("utf-8-sig")
            if "\x00" in text:
                raise DomainError("BINARY_TEXT", "Binary content is not accepted as text", 422)
            if extension == ".csv":
                fragments = []
                for number, row in enumerate(csv.reader(io.StringIO(text)), 1):
                    if len(row) == 2:
                        fragments.append({"text": f"{row[0]}: {row[1]}", "locator": {"kind": "csv", "row": number, "column": "B"}})
            else:
                fragments = [{"text": line, "locator": {"kind": "text", "line": n}}
                             for n, line in enumerate(text.splitlines(), 1) if line.strip()]
    except DomainError:
        raise
    except Exception as error:
        # Do not expose parser stack traces / internals / filesystem paths to clients.
        raise DomainError("PARSE_FAILED", f"File format could not be safely parsed ({type(error).__name__})", 422) from error
    if len(fragments) > 2000 or sum(len(f["text"]) for f in fragments) > 150_000:
        raise DomainError("TEXT_LIMIT", "Extracted document is too large", 413)
    return fragments


def parse_document(filename: str, data: bytes, document_id: str) -> dict:
    fragments = fragments_from_file(filename, data)
    sha = hashlib.sha256(data).hexdigest()
    candidates, evidence, issues = {}, {}, []
    for index, fragment in enumerate(fragments):
        fragment["id"] = f"{document_id}:f{index:04d}"
        match = re.match(r"^\s*([^:：]+)\s*[:：]\s*(.*?)\s*$", fragment["text"])
        if not match:
            continue
        key, raw = match.group(1).strip(), match.group(2).strip()
        key = ALIASES.get(key, key.lower())
        if key not in KNOWN:
            continue
        if key in candidates and candidates[key] != raw:
            candidates[key] = None
            evidence.pop(key, None)
            issues.append(f"CONFLICT:{key}")
            continue
        if key in candidates and candidates[key] is None:
            continue
        candidates[key] = raw
        evidence[key] = {"kind": "source", "document_id": document_id, "document_sha256": sha,
                         "fragment_id": fragment["id"], "text": fragment["text"], **fragment["locator"]}
    values = {}
    for key, raw in candidates.items():
        if raw is None or raw.lower() in {"", "unknown", "未知", "待定", "null", "none", "n/a"}:
            continue
        if key == "delivery_days":
            try:
                raw = int(raw)
            except ValueError:
                issues.append(f"INVALID:{key}")
                continue
        if key == "tax_mode":
            raw = {"含税": "included", "不含税": "excluded", "未税": "excluded"}.get(raw, raw.lower())
        if key in {"unit_price", "shipping_cost", "discount", "quantity"} and isinstance(raw, str):
            if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", raw):
                raw = raw.replace(",", "")
        try:
            candidate = QuoteValues.model_validate({key: raw})
            values[key] = candidate.model_dump(mode="json")[key]
        except (ValidationError, ValueError):
            issues.append(f"INVALID:{key}")
    normalized = QuoteValues.model_validate(values).model_dump(mode="json")
    for key in list(evidence):
        if normalized.get(key) is None or (key == "tax_mode" and normalized[key] == "unknown"):
            evidence.pop(key, None)
    if not evidence:
        raise DomainError("NO_RECOGNIZED_FIELDS", "No supported quote key/value fields were found; use a bundled template", 422)
    return {"values": normalized, "evidence": evidence, "fragments": fragments,
            "issues": sorted(set(issues)), "sha256": sha, "parser": "key-value-v1"}
