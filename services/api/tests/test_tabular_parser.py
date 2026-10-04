"""Adversarial contracts for bounded, explicit one-row table imports."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile
from xml.sax.saxutils import escape

import pytest

from procureflow.errors import DomainError
from procureflow.tabular import MAX_INPUT_BYTES, WORKER, isolated_parse_document, map_table, parse_table
from conftest import FIXTURES


HEADERS = ["supplier_id", "sku", "quantity", "uom", "unit_price", "tax_mode", "tax_rate",
           "shipping_cost", "discount", "delivery_days", "currency"]
VALUES = ["SUP-A", "STAND-01", "20", "EA", "1200.00", "included", "0.13", "800.00", "0.00", "7", "CNY"]


def csv_bytes(rows=None):
    import csv
    stream = io.StringIO()
    csv.writer(stream).writerows(rows or [HEADERS, VALUES])
    return stream.getvalue().encode("utf-8")


def workbook(sheets=None, *, formula=None, extra=None):
    """Minimal OOXML fixture with native addresses, no authoring dependency."""
    sheets = sheets or [("报价", [(1, HEADERS), (2, VALUES)])]
    stream = io.BytesIO()
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{namespace}" xmlns:r="{relationship}"><sheets>' +
                        "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="r{i}"/>' for i, (name, _) in enumerate(sheets, 1)) +
                        '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
                        "".join(f'<Relationship Id="r{i}" Target="worksheets/sheet{i}.xml" Type="{relationship}/worksheet"/>' for i in range(1, len(sheets) + 1)) + '</Relationships>')
        for i, (_, rows) in enumerate(sheets, 1):
            content = f'<worksheet xmlns="{namespace}"><sheetData>'
            for number, values in rows:
                content += f'<row r="{number}">'
                for column, value in enumerate(values, 1):
                    address = f'{chr(64 + column)}{number}'
                    if formula == address:
                        content += f'<c r="{address}"><f>1+1</f><v>2</v></c>'
                    else:
                        content += f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                content += '</row>'
            content += '</sheetData></worksheet>'
            archive.writestr(f"xl/worksheets/sheet{i}.xml", content)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return stream.getvalue()


def mapped(data=None, filename="quote.csv", sheet=None, header=1, row=2, mapping=None):
    table = parse_table(filename, data or csv_bytes())
    selected = next(item for item in table["sheets"] if sheet is None or item["name"] == sheet)
    result = map_table(table, selected["name"], header, row, mapping or selected["suggested_mapping"], "doc_test", table["sha256"])
    return table, result


def test_csv_header_suggestions_and_exact_provenance():
    data = csv_bytes()
    table, result = mapped(data)
    assert table["sheets"][0]["suggested_header_row"] == 1
    assert table["sheets"][0]["suggested_mapping"]["currency"] == "K"
    assert result["values"]["unit_price"] == "1200.00"
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    source = result["evidence"]["unit_price"]
    assert (source["sheet"], source["row"], source["column"], source["cell_range"], source["label_cell"]) == ("CSV", 2, "E", "E2", "E1")
    assert source["document_sha256"] == result["sha256"]
    assert source["fragment_id"] in {fragment["id"] for fragment in result["fragments"]}
    assert source["text"] == "unit_price: 1200.00"


def test_chinese_aliases_and_explicit_percent_normalization():
    header = ["供应商编码", "型号", "数量", "单位", "单价", "税价模式", "税率", "运费", "折扣", "交期天数", "币种"]
    values = list(VALUES)
    values[4], values[5], values[6], values[10] = "1,200.00", "含税", "13%", "人民币"
    _, result = mapped(csv_bytes([header, values]))
    assert result["values"]["unit_price"] == "1200.00"
    assert result["values"]["tax_rate"] == "0.13"
    assert result["values"]["tax_mode"] == "included"
    assert result["values"]["currency"] == "CNY"
    assert result["evidence"]["tax_rate"]["text"] == "税率: 13%"


def test_csv_bom_semicolon_and_physical_multiline_provenance():
    data = '\ufeffsupplier_id;sku;unit_price\n"SUP\nA";STAND-01;12.00\n'.encode()
    _, result = mapped(data)
    evidence = result["evidence"]["unit_price"]
    assert (evidence["row"], evidence["line_start"], evidence["line_end"]) == (2, 2, 3)
    assert result["values"]["supplier_id"] == "SUP\nA"


def test_xlsx_exact_sheet_sparse_row_column_and_formula_cache_ignored():
    data = workbook([("Other", [(1, ["sku", "unit_price"]), (2, ["WRONG", "999"])]),
                     ("报价", [(3, HEADERS), (7, VALUES)])], formula="E7")
    table, result = mapped(data, "quote.xlsx", sheet="报价", header=3, row=7)
    assert result["values"]["sku"] == "STAND-01"
    assert result["values"]["unit_price"] is None
    assert "unit_price" not in result["evidence"]
    assert "FORMULA:unit_price" in result["issues"]
    assert result["evidence"]["sku"]["cell_range"] == "B7"
    assert table["sheets"][1]["rows"][1]["cells"][4]["formula"] is True


@pytest.mark.parametrize("formula", ["=1+1", "+SUM(A1)", "@SUM(A1)", "-cmd|' /C calc'!A0"])
def test_csv_formula_like_values_never_become_quote_fields(formula):
    values = list(VALUES)
    values[4] = formula
    _, result = mapped(csv_bytes([HEADERS, values]))
    assert result["values"]["unit_price"] is None
    assert "FORMULA:unit_price" in result["issues"]


def test_repeated_rows_flagged_and_never_aggregated():
    table, result = mapped(csv_bytes([HEADERS, VALUES, VALUES]))
    assert "DUPLICATE_ROW:3:2" in table["sheets"][0]["issues"]
    assert "DUPLICATE_ROW:3:2" in result["issues"]
    assert result["values"]["quantity"] == "20"


def test_one_selected_row_never_inherits_other_sku_currency_or_blank_value():
    other = list(VALUES)
    other[1], other[4], other[10] = "OTHER-02", "99.00", "USD"
    selected = list(VALUES)
    selected[7] = ""
    _, result = mapped(csv_bytes([HEADERS, other, selected]), row=3)
    assert result["values"]["sku"] == "STAND-01"
    assert result["values"]["currency"] == "CNY"
    assert result["values"]["unit_price"] == "1200.00"
    assert result["values"]["shipping_cost"] is None
    assert "MISSING:shipping_cost" in result["issues"]


@pytest.mark.parametrize("mapping", [{}, {"unknown": "A"}, {"sku": "Z"}, {"sku": "b"}, {"sku": 2},
                                     {"sku": "B", "supplier_id": "B"}, {"sku": "AAA"}, {"sku": ["B", "C"]}])
def test_malformed_mapping_is_rejected(mapping):
    table = parse_table("quote.csv", csv_bytes())
    with pytest.raises(DomainError, match="Map|Mapping") as exc:
        map_table(table, "CSV", 1, 2, mapping, "doc", table["sha256"])
    assert exc.value.code == "INVALID_MAPPING"


@pytest.mark.parametrize("header,row", [(True, 2), (1, True), (1, 1), (1, 3), (0, 2)])
def test_invalid_row_selections_rejected(header, row):
    table = parse_table("quote.csv", csv_bytes())
    with pytest.raises(DomainError) as exc:
        map_table(table, "CSV", header, row, {"sku": "B"}, "doc", table["sha256"])
    assert exc.value.code == "INVALID_MAPPING"


def test_unknown_sheet_and_wrong_source_hash_rejected():
    table = parse_table("quote.csv", csv_bytes())
    with pytest.raises(DomainError) as exc:
        map_table(table, "Sheet1", 1, 2, {"sku": "B"}, "doc", table["sha256"])
    assert exc.value.code == "INVALID_MAPPING"
    with pytest.raises(DomainError) as exc:
        map_table(table, "CSV", 1, 2, {"sku": "B"}, "doc", "f" * 64)
    assert exc.value.code == "SOURCE_HASH_MISMATCH"


@pytest.mark.parametrize("field,value,code", [("sku", "A;B", "AMBIGUOUS_SKU"), ("sku", "A\nB", "AMBIGUOUS_SKU"),
                                           ("currency", "CNY/USD", "AMBIGUOUS_CURRENCY")])
def test_ambiguous_multi_value_quote_cell_rejected(field, value, code):
    values = list(VALUES)
    values[HEADERS.index(field)] = value
    with pytest.raises(DomainError) as exc:
        mapped(csv_bytes([HEADERS, values]))
    assert exc.value.code == code


def test_duplicate_headers_are_not_silently_mapped_and_repeated_header_is_rejected():
    table = parse_table("quote.csv", csv_bytes([["sku", "unit_price", "unit price"], ["A", "1", "2"]]))
    assert "unit_price" not in table["sheets"][0]["suggested_mapping"]
    assert "AMBIGUOUS_HEADER:unit_price" in table["sheets"][0]["issues"]
    with pytest.raises(DomainError) as exc:
        mapped(csv_bytes([HEADERS, HEADERS]))
    assert exc.value.code == "AMBIGUOUS_QUOTE_ROW"


@pytest.mark.parametrize("filename,data,code", [
    ("quote.exe", b"text", "UNSUPPORTED_FILE"), ("quote.xlsx", b"not zip", "FILE_SIGNATURE"),
    ("quote.csv", b"\xff", "PARSE_FAILED"), ("quote.csv", b"\x00", "BINARY_TEXT"),
    ("quote.csv", b"", "EMPTY_TABLE"), ("quote.csv", b'a,b\n"unterminated,x', "PARSE_FAILED"),
    ("quote.csv", b"x" * (MAX_INPUT_BYTES + 1), "FILE_TOO_LARGE"),
    ("quote.csv", b"a,b\n" + b"x,y\n" * 1000, "TABLE_LIMIT"),
    ("quote.csv", b",".join([b"x"] * 65), "TABLE_LIMIT"),
])
def test_input_limits_fail_closed(filename, data, code):
    with pytest.raises(DomainError) as exc:
        parse_table(filename, data)
    assert exc.value.code == code


@pytest.mark.parametrize("extra,code", [
    ({"huge.txt": b"0" * 5_000_001}, "ARCHIVE_LIMIT"),
    ({"tiny.txt": b"0" * 100_000}, "ARCHIVE_LIMIT"),
    ({"../escape": b"x"}, "UNSAFE_ARCHIVE_PATH"),
    ({"xl/vbaProject.bin": b"macro"}, "ACTIVE_CONTENT"),
    ({"xl/embeddings/object.bin": b"object"}, "ACTIVE_CONTENT"),
    ({"xl/externalLinks/externalLink1.xml": b"link"}, "ACTIVE_CONTENT"),
    ({"xl/unused.xml": b'<!DOCTYPE x [<!ENTITY y "z">]><x/>'}, "UNSAFE_XML"),
    ({"xl/unused.xml": '<!DOCTYPE x><x/>'.encode("utf-16")}, "UNSAFE_XML"),
    ({"xl/worksheets/_rels/sheet1.xml.rels": b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>'}, "ACTIVE_CONTENT"),
])
def test_xlsx_adversarial_containers(extra, code):
    with pytest.raises(DomainError) as exc:
        parse_table("quote.xlsx", workbook(extra=extra))
    assert exc.value.code == code


def test_timeout_and_worker_crash_are_sanitized(monkeypatch):
    import procureflow.tabular as module
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(DomainError) as exc:
        parse_table("quote.csv", csv_bytes())
    assert exc.value.code == "PARSER_TIMEOUT"
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], -9, b""))
    with pytest.raises(DomainError) as exc:
        parse_table("quote.csv", csv_bytes())
    assert exc.value.code == "PARSER_LIMIT"


def test_worker_has_no_inherited_secrets_shell_or_open_descriptors(monkeypatch):
    import procureflow.tabular as module
    original = module.subprocess.run
    observed = {}
    def inspect(*args, **kwargs):
        observed.update(kwargs)
        observed["command"] = args[0]
        return original(*args, **kwargs)
    monkeypatch.setenv("PF_TEST_SECRET", "do-not-inherit")
    monkeypatch.setattr(module.subprocess, "run", inspect)
    parse_table("quote.csv", csv_bytes())
    assert observed["env"] == {"LANG": "C.UTF-8"}
    assert observed["close_fds"] is True
    assert observed["command"][1:3] == ["-I", "-B"]
    assert "shell" not in observed
    assert not Path(observed["cwd"]).exists()


def test_audit_guards_deny_network_file_and_process_operations():
    # Execute only a trusted test harness, never anything obtained from a document.
    script = '''import importlib.util, json, socket, subprocess, sys
spec = importlib.util.spec_from_file_location("worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
worker.restrict_worker()
blocked = []
for action in [lambda: open("/etc/passwd"), lambda: socket.socket(), lambda: subprocess.run(["true"])]:
    try: action()
    except PermissionError: blocked.append(True)
print(json.dumps(blocked))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(WORKER)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=5, check=True)
    assert json.loads(result.stdout) == [True, True, True]


@pytest.mark.parametrize("filename", ["supplier-a.txt", "supplier-b.csv", "supplier-c.pdf", "supplier-a.xlsx"])
def test_legacy_documents_keep_behavior_inside_resource_bounded_worker(filename):
    result = isolated_parse_document(filename, (FIXTURES / filename).read_bytes(), "doc")
    assert result["values"]["sku"] == "STAND-01"
    assert result["parser"] == "key-value-v1"


def test_recognized_header_cannot_be_mapped_to_contradictory_field():
    table = parse_table("quote.csv", csv_bytes())
    with pytest.raises(DomainError) as exc:
        map_table(table, "CSV", 1, 2, {"unit_price": "C"}, "doc", table["sha256"])
    assert exc.value.code == "HEADER_MAPPING_MISMATCH"


def test_unknown_custom_header_is_still_explicitly_mappable():
    _, result = mapped(csv_bytes([["Product reference", "Each cost"], ["ABC", "12.00"]]), mapping={"sku": "A", "unit_price": "B"})
    assert result["values"]["unit_price"] == "12.00"


@pytest.mark.parametrize("headers,values,field,code", [
    (["sku", "item code"], ["ONE", "TWO"], "sku", "AMBIGUOUS_SKU"),
    (["currency", "币种"], ["USD", "CNY"], "currency", "AMBIGUOUS_CURRENCY"),
])
def test_conflicting_identity_columns_in_one_row_rejected(headers, values, field, code):
    with pytest.raises(DomainError) as exc:
        mapped(csv_bytes([headers, values]), mapping={field: "A"})
    assert exc.value.code == code


def test_formula_spill_cached_values_are_never_trusted():
    original = workbook(formula="E2")
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith("sheet1.xml"):
                data = data.replace(b"<f>1+1</f>", b'<f t="array" ref="E2:E3">1+1</f>')
            target.writestr(name, data)
    with pytest.raises(DomainError) as exc:
        parse_table("quote.xlsx", output.getvalue())
    assert exc.value.code == "UNSUPPORTED_FORMULA_RANGE"


def test_parser_capacity_is_bounded_and_slot_released_after_error(monkeypatch):
    import threading
    import procureflow.tabular as module
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(module, "PARSER_SLOTS", slots)
    assert slots.acquire(blocking=False)
    with pytest.raises(DomainError) as exc:
        parse_table("quote.csv", csv_bytes())
    assert exc.value.code == "PARSER_BUSY"
    slots.release()
    original = module.subprocess.run
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(DomainError) as exc:
        parse_table("quote.csv", csv_bytes())
    assert exc.value.code == "PARSER_TIMEOUT"
    monkeypatch.setattr(module.subprocess, "run", original)
    assert parse_table("quote.csv", csv_bytes())["kind"] == "csv"


@pytest.mark.parametrize("mutation,code", [("utf16_dtd", "UNSAFE_XML"), ("array_formula", "UNSUPPORTED_FORMULA_RANGE")])
def test_legacy_xlsx_endpoint_uses_same_security_preflight(mutation, code):
    original = (FIXTURES / "supplier-a.xlsx").read_bytes()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith("sheet1.xml"):
                from xml.etree import ElementTree as ET
                namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                root = ET.fromstring(data)
                if mutation == "utf16_dtd":
                    data = ('<!DOCTYPE worksheet [<!ENTITY amount "1200.00">]>' + ET.tostring(root, encoding="unicode")).encode("utf-16")
                else:
                    cell = next(c for c in root.iter("{" + namespace + "}c") if c.attrib.get("r") == "B6")
                    ET.SubElement(cell, "{" + namespace + "}f", {"t": "array", "ref": "B6:B7"}).text = "1+1"
                    data = ET.tostring(root)
            target.writestr(name, data)
    with pytest.raises(DomainError) as exc:
        isolated_parse_document("quote.xlsx", output.getvalue(), "doc")
    assert exc.value.code == code


def test_actual_subprocess_wall_timeout_terminates_worker(tmp_path, monkeypatch):
    import procureflow.tabular as module
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(10)\n")
    monkeypatch.setattr(module, "WORKER", worker)
    monkeypatch.setattr(module, "PARSER_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(DomainError) as exc:
        parse_table("quote.csv", csv_bytes())
    assert exc.value.code == "PARSER_TIMEOUT"
    assert module.PARSER_SLOTS.acquire(blocking=False)
    module.PARSER_SLOTS.release()


def test_worker_posix_resource_limits_are_really_installed():
    script = '''import importlib.util, json, resource, sys
spec = importlib.util.spec_from_file_location("worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
worker.restrict_worker()
print(json.dumps([resource.getrlimit(k) for k in [resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_NOFILE, resource.RLIMIT_FSIZE, resource.RLIMIT_CORE]]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(WORKER)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=5, check=True)
    assert json.loads(result.stdout) == [[256 * 1024 * 1024] * 2, [2, 2], [32, 32], [0, 0], [0, 0]]


@pytest.mark.parametrize("route", ["table", "legacy"])
@pytest.mark.parametrize("vector", ["path", "content_type", "relationship", "root"])
def test_xlm_macro_sheets_are_rejected_by_both_upload_routes(route, vector):
    original = workbook()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if vector == "path":
                if name == "xl/worksheets/sheet1.xml":
                    name = "xl/macrosheets/sheet1.xml"
                elif name.endswith("workbook.xml.rels"):
                    data = data.replace(b"worksheets/sheet1.xml", b"macrosheets/sheet1.xml")
            elif vector == "content_type" and name == "[Content_Types].xml":
                data = b'<Types><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.ms-excel.macrosheet+xml"/></Types>'
            elif vector == "relationship" and name.endswith("workbook.xml.rels"):
                data = data.replace(b"relationships/worksheet", b"relationships/xlMacrosheet")
            elif vector == "root" and name == "xl/worksheets/sheet1.xml":
                data = data.replace(b"<worksheet", b"<macrosheet").replace(b"</worksheet>", b"</macrosheet>")
            target.writestr(name, data)
    with pytest.raises(DomainError) as exc:
        if route == "table":
            parse_table("quote.xlsx", output.getvalue())
        else:
            isolated_parse_document("quote.xlsx", output.getvalue(), "doc")
    assert exc.value.code == "ACTIVE_CONTENT"


@pytest.mark.parametrize("route", ["table", "legacy"])
@pytest.mark.parametrize("encoding", ["UTF-7", "ISO-8859-1", "UTF-16", "UTF-32"])
def test_non_utf8_declarations_rejected_even_when_xml_bytes_are_ascii(route, encoding):
    data = workbook(extra={"xl/unused.xml": f'<?xml version="1.0" encoding="{encoding}"?><x/>'.encode("ascii")})
    with pytest.raises(DomainError) as exc:
        if route == "table":
            parse_table("quote.xlsx", data)
        else:
            isolated_parse_document("quote.xlsx", data, "doc")
    assert exc.value.code == "UNSAFE_XML"
