"""Bounded table preview and explicit, provenance-preserving row extraction.

The decoder subprocess is time/resource limited and denies Python filesystem,
network and process operations. It is defense in depth, not an OS sandbox or a
claim of protection against a native-runtime exploit; hardened deployment remains
necessary. The API never accepts table JSON from the client: map only a saved,
server-parsed preview bound to its uploaded document's hash.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from decimal import Decimal

from pydantic import ValidationError

from .contracts import QuoteValues
from .errors import DomainError

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 6_000_000
PARSER_TIMEOUT_SECONDS = 5
MAX_CONCURRENT_PARSERS = 2
# Per API process only; deployment-wide quotas/rate limits remain necessary.
PARSER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_PARSERS)
WORKER = Path(__file__).with_name("parser_worker.py")
ALIASES = {
    "supplier_id": ("supplier", "supplier id", "supplier code", "vendor id", "vendor code", "供应商", "供应商编码", "供应商编号"),
    "sku": ("sku", "item code", "product code", "item number", "型号", "物料编码", "产品编码", "货号", "商品编码"),
    "quantity": ("qty", "quantity", "数量", "采购数量", "报价数量"),
    "uom": ("uom", "unit", "单位", "计量单位"),
    "unit_price": ("unit price", "price", "price per unit", "单价", "报价单价", "单价(元)", "单价（元）"),
    "tax_mode": ("tax mode", "tax price mode", "税价模式", "含税模式", "是否含税"),
    "tax_rate": ("tax rate", "vat rate", "税率", "增值税率"),
    "shipping_cost": ("shipping", "shipping cost", "freight", "freight cost", "运费", "运输费"),
    "discount": ("discount", "discount amount", "折扣", "折扣金额", "优惠金额"),
    "delivery_days": ("delivery days", "lead time", "lead time days", "交期", "交期天数", "交货天数"),
    "currency": ("currency", "currency code", "币种", "货币", "结算币种"),
}
# A table column represents one scalar field, never the aggregate lines array.
KNOWN = set(ALIASES)
HEADER_VALUES = ("supplier_id", "currency", "shipping_cost")
LINE_VALUES = ("sku", "quantity", "uom", "unit_price", "tax_mode", "tax_rate", "discount", "delivery_days")
MAX_SELECTED_ROWS = 20


def _header(value):
    return re.sub(r"[\s_-]+", "", value.strip().casefold())


HEADER_FIELDS = {_header(label): field for field, labels in ALIASES.items() for label in (*labels, field)}
UNKNOWN = {"", "unknown", "未知", "待定", "null", "none", "n/a", "-"}


def _run_worker(kind, data, *arguments):
    if not isinstance(data, bytes):
        raise DomainError("INVALID_FILE", "Upload content must be bytes", 422)
    if len(data) > MAX_INPUT_BYTES:
        raise DomainError("FILE_TOO_LARGE", "File exceeds the 2 MiB import limit", 413)
    if not PARSER_SLOTS.acquire(blocking=False):
        raise DomainError("PARSER_BUSY", "Parser capacity is busy; retry the import shortly", 503)
    try:
        # No inherited application environment/secrets, no application cwd, no
        # inherited descriptors, and no shell. -I excludes PYTHONPATH/user sites.
        with tempfile.TemporaryDirectory(prefix="pf-parser-") as directory:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(WORKER), kind, *arguments], input=data,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=directory,
                env={"LANG": "C.UTF-8"}, close_fds=True, timeout=PARSER_TIMEOUT_SECONDS,
                check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise DomainError("PARSER_TIMEOUT", "Table parser exceeded its wall-clock limit", 422) from error
    except OSError as error:
        raise DomainError("PARSER_UNAVAILABLE", "Isolated parser could not be started", 503) from error
    finally:
        PARSER_SLOTS.release()
    if completed.returncode != 0:
        raise DomainError("PARSER_LIMIT", "Isolated parser terminated without a result", 422)
    if len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise DomainError("TABLE_LIMIT", "Parser output exceeds the response limit", 413)
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("Invalid envelope")
    except (ValueError, TypeError) as error:
        raise DomainError("PARSE_FAILED", "Parser returned an invalid response", 422) from error
    if not result["ok"]:
        failure = result.get("error", {})
        raise DomainError(failure.get("code", "PARSE_FAILED"), failure.get("message", "File could not be safely parsed"),
                          failure.get("status", 422))
    return result


def isolated_parse_document(filename: str, data: bytes, document_id: str) -> dict:
    """Retain legacy extraction semantics while bounding the untrusted decoder."""
    return _run_worker("document", data, filename, document_id)["document"]


def _suggestions(sheet):
    choices, issues, seen_rows = [], [], {}
    for row in sheet["rows"]:
        candidates = {}
        for cell in row["cells"]:
            if cell["formula"]:
                issues.append(f"FORMULA_CELL:{cell['cell']}")
                continue
            field = HEADER_FIELDS.get(_header(cell["value"]))
            if field:
                candidates.setdefault(field, []).append(cell["column"])
        mapping = {key: columns[0] for key, columns in candidates.items() if len(columns) == 1}
        ambiguous = sorted(key for key, columns in candidates.items() if len(columns) > 1)
        if mapping or ambiguous:
            choices.append({"row": row["row"], "mapping": mapping, "ambiguous_fields": ambiguous})
        signature = tuple((c["column"], c["value"], c["formula"]) for c in row["cells"] if c["value"].strip())
        if signature:
            if signature in seen_rows:
                issues.append(f"DUPLICATE_ROW:{row['row']}:{seen_rows[signature]}")
            else:
                seen_rows[signature] = row["row"]
    # Suggestions aid explicit review; they never select/commit an import row.
    choices.sort(key=lambda c: (-len(c["mapping"]), c["row"]))
    top = choices[0] if choices else None
    sheet["suggested_header_row"] = top["row"] if top else None
    sheet["suggested_mapping"] = top["mapping"] if top else {}
    sheet["header_candidates"] = choices[:20]
    if top:
        issues.extend(f"AMBIGUOUS_HEADER:{field}" for field in top["ambiguous_fields"])
    if sheet.get("state") != "visible":
        issues.append("HIDDEN_SHEET")
    sheet["issues"] = sorted(set(issues))


def parse_table(filename: str, data: bytes) -> dict:
    kind = Path(filename).suffix.lower().lstrip(".")
    if kind not in {"csv", "xlsx"}:
        raise DomainError("UNSUPPORTED_FILE", "Only .csv and .xlsx table imports are supported", 415)
    table = _run_worker(kind, data)["table"]
    table.update(sha256=hashlib.sha256(data).hexdigest(), parser="tabular-v1", issues=[])
    for sheet in table["sheets"]:
        _suggestions(sheet)
        table["issues"].extend(f"{sheet['name']}:{issue}" for issue in sheet["issues"])
    return table


def _column_number(column):
    number = 0
    for char in column:
        number = number * 26 + ord(char) - 64
    return number


def _inside_merge(cell, merged_ranges):
    match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
    column, row = _column_number(match[1]), int(match[2])
    for reference in merged_ranges:
        left, right = reference.split(":")
        start, end = re.fullmatch(r"([A-Z]+)(\d+)", left), re.fullmatch(r"([A-Z]+)(\d+)", right)
        if _column_number(start[1]) <= column <= _column_number(end[1]) and int(start[2]) <= row <= int(end[2]):
            return True
    return False


def map_table(table: dict, sheet: str, header_row: int, row: int, mapping: dict,
              document_id: str, sha: str) -> dict:
    """Extract exactly one explicitly selected quote row from a stored preview.

    Unmapped/invalid/blank/formula cells stay unknown; values from other rows,
    sheets, totals or repeated headers are never inherited. Mapping errors and
    intra-row SKU/currency ambiguity fail closed before a quote is created.
    """
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha) or table.get("sha256") != sha:
        raise DomainError("SOURCE_HASH_MISMATCH", "Table preview does not match its source document", 409)
    if type(header_row) is not int or type(row) is not int or row <= header_row or header_row < 1:
        raise DomainError("INVALID_MAPPING", "Select a header row and one later quote row", 422)
    if not isinstance(sheet, str):
        raise DomainError("INVALID_MAPPING", "Select one exact sheet name", 422)
    matches = [candidate for candidate in table.get("sheets", []) if candidate["name"] == sheet]
    if len(matches) != 1:
        raise DomainError("INVALID_MAPPING", "Selected sheet was not found", 422)
    selected_sheet = matches[0]
    rows = {candidate["row"]: candidate for candidate in selected_sheet["rows"]}
    if header_row not in rows or row not in rows:
        raise DomainError("INVALID_MAPPING", "Selected header or quote row was not found", 422)
    if not isinstance(mapping, dict) or not mapping or len(mapping) > len(KNOWN):
        raise DomainError("INVALID_MAPPING", "Map supported fields to distinct table columns", 422)
    header_cells = {cell["column"]: cell for cell in rows[header_row]["cells"]}
    used = set()
    for field, column in mapping.items():
        if (field not in KNOWN or not isinstance(column, str) or not re.fullmatch(r"[A-Z]{1,2}", column)
                or _column_number(column) > 64 or column in used or column not in header_cells
                or not header_cells[column]["value"].strip() or header_cells[column]["formula"]):
            raise DomainError("INVALID_MAPPING", "Mapping contains an unknown field, invalid or duplicate column", 422)
        recognized = HEADER_FIELDS.get(_header(header_cells[column]["value"]))
        if recognized is not None and recognized != field:
            raise DomainError("HEADER_MAPPING_MISMATCH", "Mapped field contradicts the recognized column header", 422)
        used.add(column)
        if _inside_merge(f"{column}{header_row}", selected_sheet.get("merged_ranges", [])) or _inside_merge(
                f"{column}{row}", selected_sheet.get("merged_ranges", [])):
            raise DomainError("AMBIGUOUS_MERGED_CELL", "Mapped header and quote cells must not be merged", 422)
    cells = {cell["column"]: cell for cell in rows[row]["cells"]}
    if not any(cells.get(column, {}).get("value", "").strip() for column in mapping.values()):
        raise DomainError("EMPTY_QUOTE_ROW", "Selected row has no mapped values", 422)
    if sum(HEADER_FIELDS.get(_header(cells.get(column, {}).get("value", ""))) == field
           for field, column in mapping.items()) >= min(2, len(mapping)):
        raise DomainError("AMBIGUOUS_QUOTE_ROW", "Selected row appears to be another header", 422)
    # Conflicting duplicate identity/currency headers are not resolved by hiding
    # another column. One row may represent only one product and one currency.
    for field, code in (("sku", "AMBIGUOUS_SKU"), ("currency", "AMBIGUOUS_CURRENCY")):
        distinct = set()
        for column, header in header_cells.items():
            candidate = cells.get(column)
            if HEADER_FIELDS.get(_header(header["value"])) != field or not candidate or candidate["formula"]:
                continue
            raw = candidate["value"].strip()
            if raw.lower() not in UNKNOWN:
                if field == "currency":
                    raw = {"人民币": "CNY", "RMB": "CNY", "美元": "USD", "欧元": "EUR"}.get(raw.upper(), raw.upper())
                distinct.add(raw)
        if len(distinct) > 1:
            raise DomainError(code, "Selected row contains conflicting product or currency columns", 422)
    values, evidence, fragments = {}, {}, []
    issues = [issue for issue in selected_sheet.get("issues", []) if not issue.startswith("FORMULA_CELL:")]
    if rows[row].get("hidden"):
        issues.append("HIDDEN_QUOTE_ROW")
    for field, column in mapping.items():
        cell = cells.get(column)
        if cell is None:
            issues.append(f"MISSING:{field}")
            continue
        original, raw = cell["value"], cell["value"].strip()
        locator = {"kind": table["kind"], "sheet": sheet, "row": row, "column": column,
                   "cell_range": cell["cell"], "header_row": header_row,
                   "label_cell": f"{column}{header_row}"}
        if table["kind"] == "csv":
            locator.update(line_start=rows[row].get("line_start", row), line_end=rows[row].get("line_end", row))
        fragment = {"id": f"{document_id}:f{len(fragments):04d}",
                    "text": f"{header_cells[column]['value']}: {original}", "locator": locator}
        fragments.append(fragment)
        if cell["formula"] or cell.get("error"):
            issues.append(f"{'FORMULA' if cell['formula'] else 'INVALID'}:{field}")
            continue
        if raw.lower() in UNKNOWN:
            issues.append(f"MISSING:{field}")
            continue
        if field == "sku" and re.search(r"[,;\n\r|]", raw):
            raise DomainError("AMBIGUOUS_SKU", "One mapped SKU cell must identify exactly one product", 422)
        if field == "currency":
            raw = {"人民币": "CNY", "RMB": "CNY", "美元": "USD", "欧元": "EUR"}.get(raw.upper(), raw.upper())
            if not re.fullmatch(r"[A-Z]{3}", raw):
                raise DomainError("AMBIGUOUS_CURRENCY", "Use one three-letter currency code in the selected row", 422)
        if field == "tax_mode":
            raw = {"含税": "included", "不含税": "excluded", "未税": "excluded"}.get(raw, raw.lower())
        if field in {"unit_price", "shipping_cost", "discount", "quantity"} and re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", raw):
            raw = raw.replace(",", "")
        if field == "tax_rate" and re.fullmatch(r"\d+(?:\.\d+)?%", raw):
            raw = str(Decimal(raw[:-1]) / 100)
        if field == "delivery_days":
            if not re.fullmatch(r"\d+", raw):
                issues.append(f"INVALID:{field}")
                continue
            raw = int(raw)
        try:
            candidate = QuoteValues.model_validate({field: raw}).model_dump(mode="json")[field]
        except (ValidationError, ValueError):
            issues.append(f"INVALID:{field}")
            continue
        if candidate is None or (field == "tax_mode" and candidate == "unknown"):
            continue
        values[field] = candidate
        evidence[field] = {"kind": "source", "document_id": document_id, "document_sha256": sha,
                           "fragment_id": fragment["id"], "text": fragment["text"], **locator}
    if not evidence:
        raise DomainError("NO_RECOGNIZED_FIELDS", "Selected row contains no valid mapped quote values", 422)
    return {"values": QuoteValues.model_validate(values).model_dump(mode="json"), "evidence": evidence,
            "fragments": fragments, "issues": sorted(set(issues)), "sha256": sha, "parser": "tabular-v1"}


def map_table_rows(table: dict, sheet: str, header_row: int, rows: list[int], mapping: dict,
                   document_id: str, sha: str) -> dict:
    """Combine explicitly selected rows without weakening single-row safeguards.

    Header fields must agree across every selected row, including unknowns.
    Freight is one quote-level amount, not a per-line amount to sum. Each line
    retains its own source cells; no value is carried into another row.
    """
    if (not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SELECTED_ROWS
            or any(type(row) is not int or not 1 <= row <= 10000 for row in rows)
            or len(set(rows)) != len(rows)):
        raise DomainError("INVALID_MAPPING", "Select between one and twenty distinct quote rows", 422)
    parsed_rows = [map_table(table, sheet, header_row, row, mapping, document_id, sha) for row in rows]
    first = parsed_rows[0]["values"]
    for field in HEADER_VALUES:
        def comparable(value):
            return Decimal(value) if field == "shipping_cost" and value is not None else value
        if any(comparable(item["values"].get(field)) != comparable(first.get(field)) for item in parsed_rows[1:]):
            raise DomainError("IMPORT_HEADER_MISMATCH",
                              f"All selected rows must have the same {field}; unknown values cannot be inherited", 422)
    known_skus = [item["values"]["sku"] for item in parsed_rows if item["values"].get("sku") is not None]
    if len(set(known_skus)) != len(known_skus):
        raise DomainError("IMPORT_DUPLICATE_SKU", "Selected rows must contain distinct SKUs; duplicate SKUs are not combined", 422)

    values = {field: first.get(field) for field in HEADER_VALUES}
    values["lines"] = [{field: item["values"].get(field) for field in LINE_VALUES} for item in parsed_rows]
    evidence, fragments, issues = {}, [], []
    header_sources = {field: [] for field in HEADER_VALUES}
    sheet_id = hashlib.sha256(sheet.encode("utf-8")).hexdigest()[:12]
    for index, (row_number, item) in enumerate(zip(rows, parsed_rows)):
        # Cell-address IDs remain stable if a buyer reorders the selected rows
        # or mapping fields, and remain distinct across worksheets.
        fragment_ids = {}
        for fragment in item["fragments"]:
            stable_id = f"{document_id}:s{sheet_id}:r{row_number:05d}:{fragment['locator']['column']}"
            fragment_ids[fragment["id"]] = stable_id
            fragments.append({**fragment, "id": stable_id})
        for field, source in item["evidence"].items():
            source = {**source, "fragment_id": fragment_ids[source["fragment_id"]]}
            if field in HEADER_VALUES:
                header_sources[field].append(source)
            else:
                evidence[f"lines.{index}.{field}"] = source
        for issue in item["issues"]:
            parts = issue.split(":", 1)
            if len(parts) == 2 and parts[0] in {"MISSING", "INVALID", "FORMULA"}:
                field = parts[1]
                issues.append(f"{parts[0]}:lines.{index}.{field}" if field in LINE_VALUES else issue)
            else:
                issues.append(issue)
    for field, sources in header_sources.items():
        if sources:
            evidence[field] = {**sources[0], "sources": sources}
    return {"values": QuoteValues.model_validate(values).model_dump(mode="json"), "evidence": evidence,
            "fragments": fragments, "issues": sorted(set(issues)), "sha256": sha, "parser": "tabular-v1"}
