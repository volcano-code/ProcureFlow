"""Synthetic multi-row import proofs; no model, ERP, or external network calls."""
from __future__ import annotations

import csv
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import BUYER, request
from procureflow.app import create_app
from procureflow.db import Database, DocumentRow, QuoteRow, QuoteVersionRow, TableImportRow
from procureflow.erp import MockERP
from procureflow.errors import DomainError
from procureflow.tabular import LINE_VALUES, map_table_rows, parse_table
from test_table_imports import analyze, confirm_import, confirm_quote, current_request, quotes, upload_table
from test_tabular_parser import workbook


FIELDS = ("supplier_id", "sku", "quantity", "uom", "unit_price", "tax_mode", "tax_rate",
          "shipping_cost", "discount", "delivery_days", "currency")
MAPPING = {field: chr(65 + index) for index, field in enumerate(FIELDS)}
FIRST = dict(zip(FIELDS, ("SUP-A", "STAND-01", "2", "EA", "10.00", "included", "0.13",
                          "7.00", "1.00", "7", "CNY")))
SECOND = {**FIRST, "sku": "DESK-02", "quantity": "3", "unit_price": "20.00", "tax_mode": "excluded",
          "tax_rate": "0.10", "discount": "2.00", "delivery_days": "9"}


def csv_source(*rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(FIELDS)
    writer.writerows([[item[field] for field in FIELDS] for item in rows or (FIRST, SECOND)])
    return output.getvalue().encode("utf-8")


def multi_request(client):
    response = client.post("/api/v1/requests", headers=BUYER, json={
        "title": "Synthetic two-item purchase", "lines": [
            {"sku": FIRST["sku"], "quantity": FIRST["quantity"], "uom": "EA"},
            {"sku": SECOND["sku"], "quantity": SECOND["quantity"], "uom": "EA"},
        ], "budget": "1000.00", "max_delivery_days": 14,
    })
    assert response.status_code == 201, response.text
    return response.json()


def preview_response(client, draft, **changes):
    command = {"expected_revision": draft["revision"], "sheet": draft["sheets"][0]["name"],
               "header_row": 1, "rows": [2, 3], "mapping": MAPPING, **changes}
    return client.post(f"/api/v1/table-imports/{draft['id']}/preview", headers=BUYER, json=command)


def preview(client, draft, **changes):
    response = preview_response(client, draft, **changes)
    assert response.status_code == 200, response.text
    return response.json()


def parsed_rows(source=None, *, filename="quote.csv", sheet="CSV", rows=None, mapping=None):
    table = parse_table(filename, csv_source() if source is None else source)
    return map_table_rows(table, sheet, 1, [2, 3] if rows is None else rows,
                          MAPPING if mapping is None else mapping, "doc_multi", table["sha256"])


def test_multi_row_import_preserves_each_cell_and_counts_quote_shipping_once(system):
    client, service, erp = system
    req = multi_request(client)
    source = csv_source()
    before = current_request(client, req["id"])
    mapped = preview(client, upload_table(client, req["id"], source))
    assert mapped["selection"]["rows"] == [2, 3] and "row" not in mapped["selection"]
    assert mapped["values"]["shipping_cost"] == "7.00"
    assert [line["sku"] for line in mapped["values"]["lines"]] == ["STAND-01", "DESK-02"]
    assert mapped["values"]["quantity"] is None and mapped["values"]["unit_price"] is None
    assert mapped["can_confirm"] and quotes(client, req["id"]) == []
    assert current_request(client, req["id"]) == before
    quote = confirm_import(client, mapped)
    assert quote["confirmed_by"] is None and not quote["calculation"]["eligible"]
    confirmed = confirm_quote(client, quote)
    cost = confirmed["calculation"]
    assert cost["eligible"] and cost["total"] == "89.80" and cost["shipping"] == "7.00"
    assert cost["goods"] == "80.00" and cost["discount"] == "3.00" and cost["added_tax"] == "5.80"
    comparison = analyze(client, req["id"])
    assert comparison["proposal"]["quote_id"] == quote["id"]
    assert comparison["proposal"]["total"] == "89.80" and erp.count() == 0

    document = client.get(f"/api/v1/documents/{quote['document_id']}/evidence", headers=BUYER).json()
    assert document["sha256"] == hashlib.sha256(source).hexdigest()
    fragment_ids = {item["id"] for item in document["fragments"]}
    assert len(fragment_ids) == len(document["fragments"]) == 2 * len(FIELDS)
    for index, row in enumerate((2, 3)):
        for field in LINE_VALUES:
            evidence = quote["evidence"][f"lines.{index}.{field}"]
            assert evidence["document_id"] == quote["document_id"]
            assert evidence["document_sha256"] == document["sha256"]
            assert evidence["fragment_id"] in fragment_ids
            assert (evidence["row"], evidence["column"], evidence["cell_range"]) == (row, MAPPING[field], f"{MAPPING[field]}{row}")
    for field in ("supplier_id", "currency", "shipping_cost"):
        evidence = quote["evidence"][field]
        assert [item["row"] for item in evidence["sources"]] == [2, 3]
        assert all(item["fragment_id"] in fragment_ids for item in evidence["sources"])
    with service.db.transaction() as session:
        assert len(list(session.scalars(select(QuoteRow)))) == 1


def test_fragment_ids_stable_under_selection_order_and_mapping_order():
    first = parsed_rows()
    second = parsed_rows(rows=[3, 2], mapping=dict(reversed(list(MAPPING.items()))))
    assert {item["id"] for item in first["fragments"]} == {item["id"] for item in second["fragments"]}
    assert first["evidence"]["lines.0.sku"]["fragment_id"] == second["evidence"]["lines.1.sku"]["fragment_id"]
    assert second["values"]["lines"][0]["sku"] == "DESK-02"


def test_xlsx_multiple_rows_do_not_inherit_formula_cache_or_other_rows():
    source = workbook([("Selected", [(1, list(FIELDS)), (2, list(FIRST.values())), (3, [SECOND[f] for f in FIELDS]),
                                     (4, ["MALICIOUS"] * len(FIELDS))]),
                       ("Other", [(1, list(FIELDS)), (2, list(FIRST.values())), (3, [SECOND[f] for f in FIELDS])])], formula="E3")
    result = parsed_rows(source, filename="quote.xlsx", sheet="Selected")
    assert result["values"]["lines"][0]["unit_price"] == "10.00"
    assert result["values"]["lines"][1]["unit_price"] is None
    assert "lines.1.unit_price" not in result["evidence"]
    assert "FORMULA:lines.1.unit_price" in result["issues"]
    assert all(item["locator"]["row"] in {2, 3} for item in result["fragments"])
    assert all(item["locator"]["sheet"] == "Selected" for item in result["fragments"])
    other = parsed_rows(source, filename="quote.xlsx", sheet="Other")
    assert {item["id"] for item in result["fragments"]}.isdisjoint({item["id"] for item in other["fragments"]})


@pytest.mark.parametrize("field,value", [("supplier_id", "SUP-B"), ("currency", "USD"), ("shipping_cost", "8.00"),
                                         ("supplier_id", ""), ("currency", ""), ("shipping_cost", "")])
def test_mixed_header_values_reject_atomically(system, field, value):
    client, _, erp = system
    req = multi_request(client)
    draft = upload_table(client, req["id"], csv_source(FIRST, {**SECOND, field: value}))
    before = current_request(client, req["id"])
    response = preview_response(client, draft)
    assert response.status_code == 422 and response.json()["error"]["code"] == "IMPORT_HEADER_MISMATCH"
    assert client.get(f"/api/v1/table-imports/{draft['id']}", headers=BUYER).json() == draft
    assert current_request(client, req["id"]) == before and quotes(client, req["id"]) == [] and erp.count() == 0


def test_equal_shipping_scales_and_currency_aliases_agree_without_summing():
    result = parsed_rows(csv_source(FIRST, {**SECOND, "shipping_cost": "7", "currency": "人民币"}))
    assert result["values"]["shipping_cost"] == "7.00" and result["values"]["currency"] == "CNY"


@pytest.mark.parametrize("field", ["supplier_id", "currency", "shipping_cost"])
def test_all_missing_header_values_remain_incomplete_and_never_zero(system, field):
    client, _, erp = system
    req = multi_request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source({**FIRST, field: ""}, {**SECOND, field: ""})))
    assert mapped["values"][field] is None and field not in mapped["evidence"]
    quote = confirm_quote(client, confirm_import(client, mapped))
    assert not quote["calculation"]["eligible"] and analyze(client, req["id"])["proposal"] is None
    assert erp.count() == 0


@pytest.mark.parametrize("changes,code", [({"sku": "STAND-01"}, "IMPORT_DUPLICATE_SKU"),
                                          ({"sku": "EXTRA-03"}, "IMPORT_SKU_MISMATCH"),
                                          ({"uom": "BOX"}, "IMPORT_UNSUPPORTED_UNIT"),
                                          ({"sku": "DESK-02;EXTRA-03"}, "AMBIGUOUS_SKU")])
def test_invalid_selected_line_identity_rejects_without_import(system, changes, code):
    client, _, _ = system
    req = multi_request(client)
    draft = upload_table(client, req["id"], csv_source(FIRST, {**SECOND, **changes}))
    response = preview_response(client, draft)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert quotes(client, req["id"]) == []


@pytest.mark.parametrize("changes", [{"sku": "unknown"}, {"quantity": ""}, {"unit_price": "=1+1"},
                                     {"tax_mode": "unknown"}, {"tax_rate": "unknown"}, {"discount": ""}])
def test_unknown_line_values_survive_import_and_block_comparison(system, changes):
    client, _, erp = system
    req = multi_request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source(FIRST, {**SECOND, **changes})))
    quote = confirm_quote(client, confirm_import(client, mapped))
    assert not quote["calculation"]["eligible"]
    assert analyze(client, req["id"])["proposal"] is None and erp.count() == 0
    field = next(iter(changes))
    assert f"lines.1.{field}" not in mapped["evidence"]


@pytest.mark.parametrize("selection", [{"rows": [2]}, {"rows": None, "row": 2}])
def test_partial_request_coverage_is_importable_but_ineligible(system, selection):
    client, _, erp = system
    req = multi_request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source()), **selection)
    assert len(mapped["values"]["lines"]) == 1
    quote = confirm_quote(client, confirm_import(client, mapped))
    assert not quote["calculation"]["eligible"]
    assert quote["calculation"]["coverage"]["missing_skus"] == ["DESK-02"]
    assert analyze(client, req["id"])["proposal"] is None and erp.count() == 0


@pytest.mark.parametrize("selection", [{"rows": []}, {"rows": [2, 2]}, {"rows": [True, 3]}, {"rows": ["2", 3]},
                                       {"rows": [0, 3]}, {"rows": [2, 10001]}, {"rows": list(range(2, 23))},
                                       {"rows": [2, 3], "row": 2}, {"rows": None}, {"rows": [1, 2]},
                                       {"rows": [2, 999]}, {"mapping": {"lines": "A"}}])
def test_invalid_multi_row_selection_has_no_state_changes(system, selection):
    client, _, _ = system
    req = multi_request(client)
    draft = upload_table(client, req["id"], csv_source())
    response = preview_response(client, draft, **selection)
    assert response.status_code == 422, response.text
    assert client.get(f"/api/v1/table-imports/{draft['id']}", headers=BUYER).json() == draft
    assert quotes(client, req["id"]) == []


@pytest.mark.parametrize("rows", [[], [2, 2], [True, 3], ["2", 3], [2, 10001], list(range(2, 23))])
def test_multi_row_mapper_defensively_enforces_selection_bounds(rows):
    with pytest.raises(DomainError) as error:
        parsed_rows(rows=rows)
    assert error.value.code == "INVALID_MAPPING"


def test_multi_row_import_revision_request_staleness_and_idempotent_confirmation(system):
    client, service, erp = system
    req = multi_request(client)
    draft = upload_table(client, req["id"], csv_source())
    mapped = preview(client, draft)
    body = {key: req[key] for key in ("title", "lines", "budget", "max_delivery_days", "currency")}
    body.update(expected_version=req["version"], title="Updated two-item request")
    changed = client.put(f"/api/v1/requests/{req['id']}", headers=BUYER, json=body)
    assert changed.status_code == 200, changed.text
    response = client.post(f"/api/v1/table-imports/{draft['id']}/confirm", headers=BUYER,
                           json={"expected_revision": mapped["revision"], "acknowledge": True})
    assert response.status_code == 409 and response.json()["error"]["code"] == "IMPORT_REQUEST_STALE"
    refreshed = preview(client, mapped, rows=[3, 2])
    response = client.post(f"/api/v1/table-imports/{draft['id']}/confirm", headers=BUYER,
                           json={"expected_revision": mapped["revision"], "acknowledge": True})
    assert response.status_code == 409 and response.json()["error"]["code"] == "VERSION_CONFLICT"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: confirm_import(client, refreshed), range(4)))
    assert len({item["id"] for item in results}) == 1 and len(quotes(client, req["id"])) == 1
    assert results[0]["values"]["lines"][0]["sku"] == "DESK-02" and erp.count() == 0
    with service.db.transaction() as session:
        assert len(list(session.scalars(select(DocumentRow)))) == 1
        assert len(list(session.scalars(select(QuoteVersionRow)))) == 1


def test_multi_row_preview_survives_restart(system):
    client, service, erp = system
    req = multi_request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source()))
    database = Database(service.settings.database_url)
    try:
        app = create_app(settings=service.settings, database=database, erp=MockERP(erp.path))
        with TestClient(app) as restarted:
            response = restarted.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER)
            assert response.status_code == 200 and response.json() == mapped
            quote = confirm_import(restarted, mapped)
            assert quote["values"] == mapped["values"] and quote["evidence"] == mapped["evidence"]
    finally:
        database.engine.dispose()


def test_multi_row_source_tampering_rejects_preview_and_confirmation(system):
    client, service, erp = system
    req = multi_request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source()))
    with service.db.transaction() as session:
        path = service.document_dir / session.get(TableImportRow, mapped["id"]).storage_key
    path.write_bytes(csv_source(FIRST, {**SECOND, "unit_price": "0.01"}))
    for response in (preview_response(client, mapped), client.post(
            f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER,
            json={"expected_revision": mapped["revision"], "acknowledge": True})):
        assert response.status_code == 409 and response.json()["error"]["code"] == "SOURCE_INTEGRITY_FAILED"
    assert quotes(client, req["id"]) == [] and erp.count() == 0


def test_legacy_row_still_emits_legacy_scalar_quote(system):
    client, _, _ = system
    req = request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source(FIRST)), rows=None, row=2)
    assert mapped["selection"]["row"] == 2 and "rows" not in mapped["selection"]
    assert mapped["values"]["sku"] == "STAND-01" and "lines" not in mapped["values"]
    assert "sku" in mapped["evidence"] and not any(key.startswith("lines.") for key in mapped["evidence"])


def test_twenty_rows_are_supported_without_implicit_extra_selection(system):
    client, _, erp = system
    selected = [{**FIRST, "sku": f"PART-{index:02d}"} for index in range(20)]
    response = client.post("/api/v1/requests", headers=BUYER, json={
        "title": "Synthetic maximum basket", "lines": [
            {"sku": line["sku"], "quantity": line["quantity"]} for line in selected], "budget": "1000.00",
    })
    assert response.status_code == 201, response.text
    req = response.json()
    # A final unrelated row is visible but is never imported without selection.
    source = csv_source(*selected, {**FIRST, "sku": "NOT-REQUESTED", "shipping_cost": "999.00"})
    mapped = preview(client, upload_table(client, req["id"], source), rows=list(range(2, 22)))
    assert len(mapped["values"]["lines"]) == 20
    quote = confirm_quote(client, confirm_import(client, mapped))
    assert quote["calculation"]["eligible"] and quote["calculation"]["total"] == "387.00"
    assert len(quote["evidence"]["shipping_cost"]["sources"]) == 20 and erp.count() == 0


def test_conflicting_hidden_identity_column_in_any_selected_row_still_rejects():
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([*FIELDS, "sku"])
    writer.writerow([*[FIRST[field] for field in FIELDS], FIRST["sku"]])
    writer.writerow([*[SECOND[field] for field in FIELDS], "CONFLICT"])
    with pytest.raises(DomainError) as error:
        parsed_rows(stream.getvalue().encode("utf-8"))
    assert error.value.code == "AMBIGUOUS_SKU"


@pytest.mark.parametrize("state", ["PAUSED", "RECOVERY"])
def test_multi_row_import_respects_write_and_recovery_fences(system, state):
    from procureflow.db import SystemStateRow
    from procureflow.maintenance import pause_writes

    client, service, erp = system
    req = multi_request(client)
    mapped = preview(client, upload_table(client, req["id"], csv_source()))
    pause_writes(service.db)
    if state == "RECOVERY":
        with service.db.operator_transaction() as session:
            system_state = session.get(SystemStateRow, 1)
            system_state.state, system_state.restore_id = "RECOVERY", "synthetic-multi-row-recovery"
    before = client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER).json()
    assert before["values"] == mapped["values"]
    for response in (preview_response(client, mapped), client.post(
            f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER,
            json={"expected_revision": mapped["revision"], "acknowledge": True})):
        assert response.status_code == 503 and response.json()["error"]["code"] == "WRITES_PAUSED"
    assert client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER).json() == before
    assert quotes(client, req["id"]) == [] and erp.count() == 0
