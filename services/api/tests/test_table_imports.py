"""Offline integration proofs for the durable, explicitly mapped table-import boundary.

These tests exercise the production API, database, evidence and existing procurement
workflow. The final order is a MockERP draft; no provider or ERP network calls occur.
"""
from __future__ import annotations

import csv
import hashlib
import io
import stat
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import zipfile
from xml.sax.saxutils import escape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import APPROVER, AUDITOR, BUYER, OTHER, approved, enqueue, publish_policy, request, upload
from procureflow.agent import ReadOnlyAgent
from procureflow.app import create_app
from procureflow.db import Database, DocumentRow, QuoteRow, QuoteVersionRow
from procureflow.erp import MockERP
from test_advice_runs import CountingAgent


FIELDS = (
    "supplier_id", "sku", "quantity", "uom", "unit_price", "tax_mode", "tax_rate",
    "shipping_cost", "discount", "delivery_days", "currency",
)
MAPPING = {field: chr(ord("A") + index) for index, field in enumerate(FIELDS)}
VALUES = (
    "SUP-A", "STAND-01", "20", "EA", "1200.00", "included", "0.13",
    "100.00", "0.00", "7", "CNY",
)


def table_bytes(**changes):
    values = dict(zip(FIELDS, VALUES))
    values.update(changes)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(FIELDS)
    writer.writerow([values[key] for key in FIELDS])
    return output.getvalue().encode("utf-8")


def upload_table(client, request_id, content=None, filename="supplier-table.csv", headers=BUYER):
    response = client.post(
        f"/api/v1/requests/{request_id}/table-imports", headers=headers,
        files={"file": (filename, table_bytes() if content is None else content)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def map_table(client, draft, mapping=None, **changes):
    command = {
        "expected_revision": draft["revision"], "sheet": draft["sheets"][0]["name"],
        "header_row": 1, "row": 2, "mapping": dict(MAPPING if mapping is None else mapping),
    }
    command.update(changes)
    response = client.post(f"/api/v1/table-imports/{draft['id']}/preview", headers=BUYER, json=command)
    assert response.status_code == 200, response.text
    return response.json()


def confirm_import(client, draft):
    response = client.post(f"/api/v1/table-imports/{draft['id']}/confirm", headers=BUYER,
                           json={"expected_revision": draft["revision"], "acknowledge": True})
    assert response.status_code == 200, response.text
    return response.json()


def confirm_quote(client, quote):
    response = client.post(f"/api/v1/quotes/{quote['id']}/confirm", headers=BUYER,
                           json={"expected_version": quote["version"], "acknowledge": True})
    assert response.status_code == 200, response.text
    return response.json()


def analyze(client, request_id):
    response = client.post(f"/api/v1/requests/{request_id}/analyze", headers=BUYER, json={})
    assert response.status_code == 200, response.text
    return response.json()


def current_request(client, request_id):
    response = client.get(f"/api/v1/requests/{request_id}", headers=BUYER)
    assert response.status_code == 200, response.text
    return response.json()


def quotes(client, request_id):
    response = client.get(f"/api/v1/requests/{request_id}/quotes", headers=BUYER)
    assert response.status_code == 200, response.text
    return response.json()


def edit_request(client, request_id):
    old = current_request(client, request_id)
    command = {key: old[key] for key in (
        "title", "sku", "quantity", "uom", "budget", "max_delivery_days", "currency",
    )}
    command.update(expected_version=old["version"], title=old["title"] + " revised")
    response = client.put(f"/api/v1/requests/{request_id}", headers=BUYER, json=command)
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_mapping_confirmation_enters_existing_draft_order_workflow(system):
    client, service, erp = system
    req = request(client)
    before = current_request(client, req["id"])
    source = table_bytes()
    draft = upload_table(client, req["id"], source)
    assert draft["revision"] == 1 and draft["status"] == "OPEN"
    assert draft["request_id"] == req["id"] and draft["expires_at"]
    assert draft["selection"] is None and draft["values"] is None
    assert draft["can_confirm"] is False and draft["quote_id"] is None
    assert draft["document_sha256"] == hashlib.sha256(source).hexdigest()
    assert quotes(client, req["id"]) == []
    assert current_request(client, req["id"]) == before

    mapped = map_table(client, draft)
    assert mapped["id"] == draft["id"] and mapped["revision"] == 2
    assert mapped["selection"]["row"] == 2 and mapped["selection"]["mapping"] == MAPPING
    assert mapped["values"]["unit_price"] == "1200.00"
    assert mapped["values"]["shipping_cost"] == "100.00"
    assert mapped["can_confirm"] is True
    assert quotes(client, req["id"]) == [] and erp.count() == 0
    assert current_request(client, req["id"]) == before

    quote = confirm_import(client, mapped)
    assert quote["confirmed_by"] is None and quote["version"] == 1
    assert quote["calculation"]["eligible"] is False
    assert quote["document_sha256"] == draft["document_sha256"]
    assert analyze(client, req["id"])["proposal"] is None
    assert erp.count() == 0
    confirmed = confirm_quote(client, quote)
    assert confirmed["confirmed_by"] == "buyer-01"
    comparison = analyze(client, req["id"])
    proposal = comparison["proposal"]
    assert proposal["quote_id"] == quote["id"] and proposal["total"] == "24100.00"
    assert comparison["evaluation"]["current"] and comparison["llm_used"] is False
    response = client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
                           json={"snapshot_hash": proposal["snapshot_hash"]})
    assert response.status_code == 200, response.text
    operation = enqueue(client, req, proposal)
    result = client.post(f"/api/v1/operations/{operation['id']}/process", headers=BUYER)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "COMPLETED" and erp.count() == 1
    remote = erp.find(operation["id"])
    assert remote["docstatus"] == 0 and remote["total"] == "24100.00"
    # Replay remains a read of the original quote even after ERP locks the request.
    assert confirm_import(client, mapped)["id"] == quote["id"]
    assert erp.count() == 1
    with service.db.transaction() as session:
        assert len(list(session.scalars(select(QuoteRow)))) == 1
        assert len(list(session.scalars(select(QuoteVersionRow)))) == 1


def test_table_cell_evidence_is_bound_to_original_source_bytes(system):
    client, _, _ = system
    req = request(client)
    source = table_bytes()
    mapped = map_table(client, upload_table(client, req["id"], source))
    quote = confirm_import(client, mapped)
    response = client.get(f"/api/v1/documents/{quote['document_id']}/evidence", headers=BUYER)
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["sha256"] == hashlib.sha256(source).hexdigest()
    fragment_ids = {fragment["id"] for fragment in document["fragments"]}
    for field, evidence in quote["evidence"].items():
        assert evidence["document_id"] == quote["document_id"]
        assert evidence["document_sha256"] == document["sha256"]
        assert evidence["fragment_id"] in fragment_ids
        assert evidence["row"] == 2 and evidence["column"] == MAPPING[field]


def test_duplicate_upload_and_concurrent_confirm_create_exactly_one_quote(system):
    client, service, erp = system
    req = request(client)
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post(
            f"/api/v1/requests/{req['id']}/table-imports", headers=BUYER,
            files={"file": ("supplier-table.csv", table_bytes())}), range(5)))
    assert any(response.status_code == 201 for response in responses)
    uploaded = []
    for response in responses:
        assert response.status_code in {201, 503}, response.text
        if response.status_code == 503:
            assert response.json()["error"]["code"] == "PARSER_BUSY"
            # Explicit caller retry only after the concurrent batch finishes;
            # arbitrary failures and database conflicts must never be retried here.
            uploaded.append(upload_table(client, req["id"]))
        else:
            uploaded.append(response.json())
    assert len(uploaded) == 5
    assert len({item["id"] for item in uploaded}) == 1
    assert {item["revision"] for item in uploaded} == {1}
    mapped = map_table(client, uploaded[0])
    before_version = current_request(client, req["id"])["version"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        imported = list(pool.map(lambda _: confirm_import(client, mapped), range(5)))
    assert len({item["id"] for item in imported}) == 1
    assert len(quotes(client, req["id"])) == 1 and erp.count() == 0
    assert current_request(client, req["id"])["version"] == before_version + 1
    replay = upload_table(client, req["id"])
    assert replay["id"] == mapped["id"] and replay["status"] == "IMPORTED"
    assert replay["quote_id"] == imported[0]["id"]
    with service.db.transaction() as session:
        assert len(list(session.scalars(select(DocumentRow)))) == 1
        assert len(list(session.scalars(select(QuoteVersionRow)))) == 1


def test_resume_after_process_restart_preserves_mapped_revision_and_evidence(system):
    client, service, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    reopened = Database(service.settings.database_url)
    try:
        restarted = create_app(settings=service.settings, database=reopened, erp=MockERP(erp.path))
        with TestClient(restarted) as other_client:
            response = other_client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER)
            assert response.status_code == 200, response.text
            assert response.json() == mapped
            quote = confirm_import(other_client, mapped)
            assert quote["values"] == mapped["values"] and quote["confirmed_by"] is None
            assert len(quotes(client, req["id"])) == 1
    finally:
        reopened.engine.dispose()


def test_confirmation_requires_a_current_explicit_mapping(system):
    client, _, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    result = client.post(f"/api/v1/table-imports/{draft['id']}/confirm", headers=BUYER,
                         json={"expected_revision": 1, "acknowledge": True})
    assert result.status_code == 409 and result.json()["error"]["code"] == "IMPORT_NOT_PREVIEWED"
    mapped = map_table(client, draft)
    revised = map_table(client, mapped)
    assert revised["revision"] == mapped["revision"] + 1
    for action, body in (
        ("confirm", {"expected_revision": mapped["revision"], "acknowledge": True}),
        ("preview", {"expected_revision": mapped["revision"], **mapped["selection"]}),
    ):
        response = client.post(f"/api/v1/table-imports/{draft['id']}/{action}", headers=BUYER, json=body)
        assert response.status_code == 409 and response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert quotes(client, req["id"]) == []
    confirm_import(client, revised)
    response = client.post(f"/api/v1/table-imports/{draft['id']}/preview", headers=BUYER,
                           json={"expected_revision": revised["revision"], **revised["selection"]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IMPORT_ALREADY_CONFIRMED"


def test_request_edit_rejects_stale_mapping_until_user_previews_again(system):
    client, _, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    edit_request(client, req["id"])
    response = client.post(f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER,
                           json={"expected_revision": mapped["revision"], "acknowledge": True})
    assert response.status_code == 409 and response.json()["error"]["code"] == "IMPORT_REQUEST_STALE"
    assert quotes(client, req["id"]) == [] and erp.count() == 0
    refreshed = map_table(client, mapped)
    assert refreshed["revision"] == mapped["revision"] + 1
    assert confirm_import(client, refreshed)["confirmed_by"] is None


def test_expired_preview_fails_closed_and_reupload_requires_new_revision(system):
    from procureflow.db import TableImportRow

    client, service, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    with service.db.transaction(write=True) as session:
        session.get(TableImportRow, mapped["id"]).expires_at = "2000-01-01T00:00:00+00:00"
    for action, body in (
        ("confirm", {"expected_revision": mapped["revision"], "acknowledge": True}),
        ("preview", {"expected_revision": mapped["revision"], **mapped["selection"]}),
    ):
        response = client.post(f"/api/v1/table-imports/{mapped['id']}/{action}", headers=BUYER, json=body)
        assert response.status_code == 410 and response.json()["error"]["code"] == "TABLE_IMPORT_EXPIRED"
    assert quotes(client, req["id"]) == [] and erp.count() == 0
    refreshed = upload_table(client, req["id"])
    assert refreshed["id"] == mapped["id"] and refreshed["revision"] > mapped["revision"]
    assert refreshed["selection"] is None and not refreshed["can_confirm"]
    assert confirm_import(client, map_table(client, refreshed))["confirmed_by"] is None


@pytest.mark.parametrize("changes,unmapped,field", [
    ({"shipping_cost": ""}, (), "shipping_cost"),
    ({"tax_rate": "unknown", "tax_mode": "excluded"}, (), "tax_rate"),
    ({"unit_price": "=1000+200"}, (), "unit_price"),
    ({}, ("shipping_cost",), "shipping_cost"),
])
def test_missing_unmapped_and_formula_cells_never_become_known_economic_values(system, changes, unmapped, field):
    client, _, erp = system
    req = request(client)
    mapping = {key: value for key, value in MAPPING.items() if key not in unmapped}
    mapped = map_table(client, upload_table(client, req["id"], table_bytes(**changes)), mapping)
    assert mapped["values"][field] is None
    assert field not in mapped["evidence"]
    quote = confirm_import(client, mapped)
    confirm_quote(client, quote)
    comparison = analyze(client, req["id"])
    assert comparison["proposal"] is None
    assert comparison["quotes"][0]["calculation"]["eligible"] is False
    assert erp.count() == 0


def test_wrong_mapping_rejected_before_preview_mutation_and_correct_mapping_succeeds(system):
    client, _, erp = system
    req = request(client)
    draft = upload_table(client, req["id"])
    before = current_request(client, req["id"])
    mapping = {**MAPPING, "supplier_id": "E", "unit_price": "A"}
    response = client.post(f"/api/v1/table-imports/{draft['id']}/preview", headers=BUYER, json={
        "expected_revision": draft["revision"], "sheet": draft["sheets"][0]["name"],
        "header_row": 1, "row": 2, "mapping": mapping,
    })
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "HEADER_MAPPING_MISMATCH"
    assert client.get(f"/api/v1/table-imports/{draft['id']}", headers=BUYER).json() == draft
    assert current_request(client, req["id"]) == before
    assert quotes(client, req["id"]) == [] and erp.count() == 0
    # The user can explicitly correct the mapping without an implicit fallback.
    mapped = map_table(client, draft)
    assert mapped["values"]["unit_price"] == "1200.00"
    assert mapped["values"]["supplier_id"] == "SUP-A"
    quote = confirm_import(client, mapped)
    assert quote["confirmed_by"] is None and not quote["calculation"]["eligible"]
    confirm_quote(client, quote)
    assert analyze(client, req["id"])["proposal"]["total"] == "24100.00"
    assert erp.count() == 0


@pytest.mark.parametrize("changes", [
    {"mapping": {"unit_price": "ZZZ"}},
    {"mapping": {"approved_by": "A"}},
    {"mapping": {"supplier_id": "A", "sku": "A"}},
    {"sheet": "Missing sheet"},
    {"row": 1},
    {"row": 999},
    {"header_row": 999},
    {"expected_revision": True},
    {"expected_revision": "1"},
    {"approved": True},
])
def test_invalid_selection_commands_do_not_mutate_the_saved_preview(system, changes):
    client, _, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    command = {"expected_revision": draft["revision"], "sheet": draft["sheets"][0]["name"],
               "header_row": 1, "row": 2, "mapping": dict(MAPPING), **changes}
    response = client.post(f"/api/v1/table-imports/{draft['id']}/preview", headers=BUYER, json=command)
    assert response.status_code == 422, response.text
    saved = client.get(f"/api/v1/table-imports/{draft['id']}", headers=BUYER)
    assert saved.json() == draft and quotes(client, req["id"]) == []


def test_imports_are_tenant_isolated_and_only_buyers_can_mutate(system):
    client, _, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    preview_command = {"expected_revision": draft["revision"], "sheet": draft["sheets"][0]["name"],
                       "header_row": 1, "row": 2, "mapping": MAPPING}
    confirm_command = {"expected_revision": draft["revision"], "acknowledge": True}
    assert client.get(f"/api/v1/table-imports/{draft['id']}", headers=OTHER).status_code == 404
    for suffix, body in (("preview", preview_command), ("confirm", confirm_command)):
        assert client.post(f"/api/v1/table-imports/{draft['id']}/{suffix}", headers=OTHER, json=body).status_code == 404
        assert client.post(f"/api/v1/table-imports/{draft['id']}/{suffix}", headers=AUDITOR, json=body).status_code == 403
    assert client.get(f"/api/v1/table-imports/{draft['id']}", headers=AUDITOR).status_code == 200
    for headers, status in ((OTHER, 404), (AUDITOR, 403)):
        response = client.post(f"/api/v1/requests/{req['id']}/table-imports", headers=headers,
                               files={"file": ("quote.csv", table_bytes())})
        assert response.status_code == status
    assert quotes(client, req["id"]) == []


@pytest.mark.parametrize("mutation", ["new_import", "quote_revision"])
def test_import_or_quote_revision_invalidates_existing_advice_evaluation_and_approval(system, monkeypatch, mutation):
    client, _, erp = system
    req = request(client)
    old_quote = confirm_import(client, map_table(client, upload_table(client, req["id"])))
    confirm_quote(client, old_quote)
    proposal = analyze(client, req["id"])["proposal"]
    response = client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
                           json={"snapshot_hash": proposal["snapshot_hash"]})
    assert response.status_code == 200, response.text
    agent = CountingAgent()
    monkeypatch.setattr(ReadOnlyAgent, "from_env", staticmethod(agent.factory))
    version = current_request(client, req["id"])["version"]
    response = client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=BUYER,
                           json={"expected_version": version, "idempotency_key": "before-table-change"})
    assert response.status_code == 201, response.text
    advice_id = response.json()["id"]
    response = client.post(f"/api/v1/advice-runs/{advice_id}/process", headers=BUYER)
    assert response.status_code == 200 and response.json()["current"], response.text
    old_advice = response.json()
    evaluations_url = f"/api/v1/requests/{req['id']}/evaluations"
    old_evaluation = client.get(evaluations_url, headers=AUDITOR).json()[0]
    assert old_evaluation["current"]

    mapped = map_table(client, upload_table(client, req["id"], table_bytes(supplier_id="SUP-B")))
    # Merely reviewing a table must not change business input or revoke approval.
    assert current_request(client, req["id"])["version"] == version
    assert client.get(f"/api/v1/advice-runs/{advice_id}", headers=BUYER).json()["current"]
    assert client.get(evaluations_url, headers=AUDITOR).json()[0]["current"]
    assert current_request(client, req["id"])["status"] == "APPROVED"

    if mutation == "new_import":
        imported = confirm_import(client, mapped)
        assert imported["id"] != old_quote["id"] and imported["confirmed_by"] is None
    else:
        response = client.put(f"/api/v1/quotes/{old_quote['id']}", headers=BUYER, json={
            "expected_version": old_quote["version"],
            "values": {**old_quote["values"], "unit_price": "1150.00"},
            "reason": "Supplier revised the quoted amount",
        })
        assert response.status_code == 200, response.text
        assert response.json()["confirmed_by"] is None
    advice = client.get(f"/api/v1/advice-runs/{advice_id}", headers=BUYER).json()
    assert not advice["current"] and advice["stale_reason"]
    assert advice["output"] == old_advice["output"] and agent.calls == 1
    historical = client.get(evaluations_url, headers=AUDITOR).json()[0]
    assert not historical["current"] and historical["stale_reason"]
    assert historical["input_snapshot"] == old_evaluation["input_snapshot"]
    assert historical["result"] == old_evaluation["result"]
    approval = client.get(f"/api/v1/requests/{req['id']}/approvals", headers=AUDITOR).json()[0]
    assert approval["status"] == "STALE" and not approval["current"]
    assert approval["snapshot"] == proposal
    response = client.post(f"/api/v1/requests/{req['id']}/execute", headers=BUYER,
                           json={"snapshot_hash": proposal["snapshot_hash"]})
    assert response.status_code == 409 and erp.count() == 0


def workbook_bytes(*, formula=False):
    """Small valid, non-active OOXML with an irrelevant first sheet and two quote rows."""
    output = io.BytesIO()
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

    def row(number, values):
        cells = []
        for index, value in enumerate(values):
            cell = f"{chr(ord('A') + index)}{number}"
            if formula and number == 3 and index == 4:
                # A cached value must never make the unevaluated formula trustworthy.
                cells.append(f'<c r="{cell}"><f>1000+200</f><v>1200</v></c>')
            else:
                cells.append(f'<c r="{cell}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        return f'<row r="{number}">' + "".join(cells) + '</row>'

    sheet = f'<worksheet xmlns="{ns}"><sheetData>'
    sheet += row(1, ["Supplier quotation export"])
    sheet += row(2, FIELDS)
    sheet += row(3, VALUES)
    sheet += row(4, ("SUP-B", "OTHER-SKU", "1", "EA", "1.00", "included", "0.13", "0.00", "0.00", "1", "USD"))
    sheet += '</sheetData></worksheet>'
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Default Extension="xml" ContentType="application/xml"/>
          <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
          <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
          <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
        </Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
        </Relationships>''')
        archive.writestr("xl/workbook.xml", f'''<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="Readme" sheetId="1" r:id="rId1"/><sheet name="报价表" sheetId="2" r:id="rId2"/></sheets>
        </workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
          <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
        </Relationships>''')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{row(1, ["Read instructions"])}</sheetData></worksheet>')
        archive.writestr("xl/worksheets/sheet2.xml", sheet)
    return output.getvalue()


def test_xlsx_explicit_sheet_header_and_row_selection_never_merges_other_rows(system):
    client, _, erp = system
    req = request(client)
    draft = upload_table(client, req["id"], workbook_bytes(), "quotes.xlsx")
    assert [sheet["name"] for sheet in draft["sheets"]] == ["Readme", "报价表"]
    mapped = map_table(client, draft, sheet="报价表", header_row=2, row=3)
    quote = confirm_import(client, mapped)
    assert quote["values"]["sku"] == "STAND-01" and quote["values"]["currency"] == "CNY"
    assert quote["values"]["unit_price"] == "1200.00"
    assert quote["values"]["supplier_id"] == "SUP-A"
    for field, evidence in quote["evidence"].items():
        assert evidence["sheet"] == "报价表" and evidence["row"] == 3
        assert evidence["column"] == MAPPING[field]
        assert evidence["cell_range"] == MAPPING[field] + "3"
    confirm_quote(client, quote)
    assert analyze(client, req["id"])["proposal"]["total"] == "24100.00"
    assert len(quotes(client, req["id"])) == 1 and erp.count() == 0


def test_xlsx_formula_cached_value_remains_unknown_through_confirmation(system):
    client, _, erp = system
    req = request(client)
    draft = upload_table(client, req["id"], workbook_bytes(formula=True), "formula.xlsx")
    mapped = map_table(client, draft, sheet="报价表", header_row=2, row=3)
    assert mapped["values"]["unit_price"] is None
    assert "FORMULA:unit_price" in mapped["issues"]
    quote = confirm_import(client, mapped)
    confirm_quote(client, quote)
    assert analyze(client, req["id"])["proposal"] is None and erp.count() == 0


@pytest.mark.parametrize("changes", [{"sku": "OTHER-SKU"}, {"currency": "USD"}, {"uom": "BOX"}])
def test_selected_quote_outside_single_sku_cny_ea_scope_is_rejected(system, changes):
    client, _, _ = system
    req = request(client)
    draft = upload_table(client, req["id"], table_bytes(**changes))
    response = client.post(f"/api/v1/table-imports/{draft['id']}/preview", headers=BUYER, json={
        "expected_revision": 1, "sheet": draft["sheets"][0]["name"], "header_row": 1, "row": 2, "mapping": MAPPING,
    })
    assert response.status_code == 422, response.text
    assert quotes(client, req["id"]) == []


def test_source_tampering_prevents_preview_read_and_confirmation(system):
    from procureflow.db import TableImportRow

    client, service, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    with service.db.transaction() as session:
        row = session.get(TableImportRow, mapped["id"])
        source_path = service.document_dir / row.storage_key
    source_path.write_bytes(table_bytes(unit_price="1.00"))
    response = client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER)
    assert response.status_code == 409 and response.json()["error"]["code"] == "SOURCE_INTEGRITY_FAILED"
    response = client.post(f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER,
                           json={"expected_revision": mapped["revision"], "acknowledge": True})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SOURCE_INTEGRITY_FAILED"
    assert quotes(client, req["id"]) == [] and erp.count() == 0


def test_duplicate_source_hash_is_scoped_to_each_request(system):
    client, _, _ = system
    first, second = request(client), request(client)
    a = upload_table(client, first["id"])
    b = upload_table(client, second["id"])
    assert a["document_sha256"] == b["document_sha256"] and a["id"] != b["id"]
    first_quote = confirm_import(client, map_table(client, a))
    second_quote = confirm_import(client, map_table(client, b))
    assert first_quote["id"] != second_quote["id"]
    assert first_quote["document_id"] != second_quote["document_id"]


def test_existing_key_value_quote_prevents_second_import_of_the_same_source(system):
    from conftest import FIXTURES

    client, _, _ = system
    req = request(client)
    quote = upload(client, req["id"], "supplier-b.csv")
    response = client.post(f"/api/v1/requests/{req['id']}/table-imports", headers=BUYER,
                           files={"file": ("same-source.csv", (FIXTURES / "supplier-b.csv").read_bytes())})
    assert response.status_code == 409 and response.json()["error"]["code"] == "SOURCE_ALREADY_IMPORTED"
    assert [item["id"] for item in quotes(client, req["id"])] == [quote["id"]]


def test_execution_reservation_blocks_unconfirmed_preview_and_new_upload(system):
    client, _, erp = system
    req, _, proposal = approved(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    enqueue(client, req, proposal)
    saved = client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER).json()
    assert saved["can_confirm"] is False
    for suffix, body in (
        ("preview", {"expected_revision": mapped["revision"], **mapped["selection"]}),
        ("confirm", {"expected_revision": mapped["revision"], "acknowledge": True}),
    ):
        response = client.post(f"/api/v1/table-imports/{mapped['id']}/{suffix}", headers=BUYER, json=body)
        assert response.status_code == 409 and response.json()["error"]["code"] == "REQUEST_FROZEN"
    response = client.post(f"/api/v1/requests/{req['id']}/table-imports", headers=BUYER,
                           files={"file": ("other.csv", table_bytes(supplier_id="SUP-B"))})
    assert response.status_code == 409 and response.json()["error"]["code"] == "REQUEST_FROZEN"
    assert len(quotes(client, req["id"])) == 1 and erp.count() == 0


@pytest.mark.parametrize("body", [
    {"expected_revision": 2},
    {"expected_revision": 2, "acknowledge": False},
    {"expected_revision": 2, "acknowledge": True, "confirmed_by": "approver-01"},
    {"expected_revision": "2", "acknowledge": True},
    {"expected_revision": True, "acknowledge": True},
])
def test_import_confirmation_rejects_missing_consent_or_client_owned_state(system, body):
    client, _, _ = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    response = client.post(f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER, json=body)
    assert response.status_code == 422, response.text
    assert quotes(client, req["id"]) == []
    assert client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER).json() == mapped


def test_concurrent_remapping_has_one_winner_and_never_creates_a_quote(system):
    client, _, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    body = {"expected_revision": draft["revision"], "sheet": draft["sheets"][0]["name"],
            "header_row": 1, "row": 2, "mapping": MAPPING}
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(f"/api/v1/table-imports/{draft['id']}/preview",
            headers=BUYER, json=body), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409]
    saved = client.get(f"/api/v1/table-imports/{draft['id']}", headers=BUYER).json()
    assert saved["revision"] == 2 and saved["selection"]["mapping"] == MAPPING
    assert quotes(client, req["id"]) == []


def test_duplicate_import_confirmation_preserves_a_later_corrected_and_confirmed_quote(system):
    client, _, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    original = confirm_import(client, mapped)
    confirm_quote(client, original)
    response = client.put(f"/api/v1/quotes/{original['id']}", headers=BUYER, json={
        "expected_version": original["version"],
        "values": {**original["values"], "unit_price": "1100.00"},
        "reason": "Supplier confirmed the corrected unit price",
    })
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["version"] == 2 and revised["confirmed_by"] is None
    assert revised["evidence"]["unit_price"]["kind"] == "manual"
    confirmed = confirm_quote(client, revised)
    assert confirmed["version"] == 2 and confirmed["confirmed_by"] == "buyer-01"
    before = current_request(client, req["id"])
    history_url = f"/api/v1/quotes/{original['id']}/versions"
    history = client.get(history_url, headers=BUYER).json()
    assert len(history) == 2
    assert history[0]["values"]["unit_price"] == "1200.00"
    for _ in range(3):
        replay = confirm_import(client, mapped)
        assert replay == confirmed
        assert replay["values"]["unit_price"] == "1100.00"
        assert replay["evidence"]["unit_price"] == revised["evidence"]["unit_price"]
    assert current_request(client, req["id"]) == before
    assert client.get(history_url, headers=BUYER).json() == history
    assert len(quotes(client, req["id"])) == 1 and erp.count() == 0


def test_expired_imported_receipt_replays_without_reopening_or_creating_a_quote(system):
    from procureflow.db import TableImportRow

    client, service, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    imported = confirm_import(client, mapped)
    confirmed = confirm_quote(client, imported)
    before = current_request(client, req["id"])
    with service.db.transaction(write=True) as session:
        session.get(TableImportRow, mapped["id"]).expires_at = "2000-01-01T00:00:00+00:00"
    response = client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["status"] == "IMPORTED" and saved["quote_id"] == imported["id"]
    assert saved["revision"] == mapped["revision"] and saved["can_confirm"] is False
    assert confirm_import(client, mapped) == confirmed
    # Upload deduplication must also preserve an expired final receipt.
    assert upload_table(client, req["id"]) == saved
    assert current_request(client, req["id"]) == before
    assert len(quotes(client, req["id"])) == 1 and erp.count() == 0


def test_one_hundred_pending_import_cap_is_tenant_scoped_and_confirmation_frees_a_slot(system):
    from procureflow.db import TableImportRow
    from procureflow.table_imports import MAX_PENDING_IMPORTS_PER_TENANT

    client, service, _ = system
    req = request(client)
    assert MAX_PENDING_IMPORTS_PER_TENANT == 100
    drafts = [upload_table(client, req["id"], table_bytes(supplier_id=f"PENDING-{index:03}"))
              for index in range(100)]
    assert len({draft["id"] for draft in drafts}) == 100
    assert len(list(service.document_dir.iterdir())) == 100
    with service.db.transaction() as session:
        assert len(list(session.scalars(select(TableImportRow)))) == 100
    before = current_request(client, req["id"])
    response = client.post(f"/api/v1/requests/{req['id']}/table-imports", headers=BUYER,
                           files={"file": ("over-limit.csv", table_bytes(supplier_id="PENDING-100"))})
    assert response.status_code == 413 and response.json()["error"]["code"] == "TABLE_IMPORT_LIMIT"
    assert current_request(client, req["id"]) == before
    assert len(list(service.document_dir.iterdir())) == 100 and quotes(client, req["id"]) == []
    # Exact repeats do not consume a new slot, even at the cap.
    assert upload_table(client, req["id"], table_bytes(supplier_id="PENDING-000")) == drafts[0]

    another = request(client)
    response = client.post(f"/api/v1/requests/{another['id']}/table-imports", headers=BUYER,
                           files={"file": ("another-request.csv", table_bytes())})
    assert response.status_code == 413 and response.json()["error"]["code"] == "TABLE_IMPORT_LIMIT"
    response = client.post("/api/v1/requests", headers=OTHER, json={
        "title": "Other workspace request", "sku": "STAND-01", "quantity": "20",
        "budget": "30000.00", "max_delivery_days": 14,
    })
    assert response.status_code == 201, response.text
    other_req = response.json()
    other_draft = upload_table(client, other_req["id"], headers=OTHER)
    assert other_draft["status"] == "OPEN"

    confirm_import(client, map_table(client, drafts[0]))
    admitted = upload_table(client, req["id"], table_bytes(supplier_id="PENDING-100"))
    assert admitted["status"] == "OPEN" and admitted["id"] not in {draft["id"] for draft in drafts}
    with service.db.transaction() as session:
        tenant_rows = list(session.scalars(select(TableImportRow).where(TableImportRow.tenant_id == "demo")))
        assert sum(row.status == "OPEN" for row in tenant_rows) == 100
        assert sum(row.status == "IMPORTED" for row in tenant_rows) == 1


def test_uploaded_source_files_remain_private_before_and_after_import(system):
    from procureflow.db import TableImportRow

    client, service, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    with service.db.transaction() as session:
        storage_key = session.get(TableImportRow, draft["id"]).storage_key
    path = service.document_dir / storage_key
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == table_bytes()
    imported = confirm_import(client, map_table(client, draft))
    with service.db.transaction() as session:
        assert session.get(DocumentRow, imported["document_id"]).storage_key == storage_key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(list(service.document_dir.iterdir())) == 1


@pytest.mark.parametrize("publication_phase", ["before_import", "after_import"])
def test_policy_published_after_preview_binds_current_policy_without_request_version_change(system, publication_phase):
    client, _, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    quote = confirm_import(client, mapped) if publication_phase == "after_import" else None
    version = current_request(client, req["id"])["version"]
    revised_policy = publish_policy(client, budget_cap="10000.00", reason="Reduce the tenant budget cap")
    assert current_request(client, req["id"])["version"] == version
    if quote is None:
        saved = client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER).json()
        assert saved["can_confirm"] and saved["revision"] == mapped["revision"]
        quote = confirm_import(client, mapped)
    confirm_quote(client, quote)
    comparison = analyze(client, req["id"])
    assert comparison["policy"]["policy_hash"] == revised_policy["policy_hash"]
    assert comparison["evaluation"]["policy_hash"] == revised_policy["policy_hash"]
    assert comparison["evaluation"]["current"]
    assert comparison["proposal"] is None
    assert "BUDGET_EXCEEDED" in comparison["quotes"][0]["calculation"]["violations"]
    assert erp.count() == 0


def test_scheduled_policy_activation_between_preview_and_import_is_rechecked(system, monkeypatch):
    from procureflow import policies

    client, _, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    activation = datetime.now(timezone.utc) + timedelta(hours=1)
    scheduled = publish_policy(client, budget_cap="10000.00", effective_at=activation.isoformat(),
                               reason="Activate a tighter future budget cap")
    assert scheduled["status"] == "scheduled"
    assert client.get("/api/v1/policy", headers=BUYER).json()["version"] == 1
    version = current_request(client, req["id"])["version"]
    monkeypatch.setattr(policies, "now", lambda: (activation + timedelta(seconds=1)).isoformat())
    assert current_request(client, req["id"])["version"] == version
    quote = confirm_import(client, mapped)
    assert "BUDGET_EXCEEDED" in quote["calculation"]["violations"]
    confirm_quote(client, quote)
    comparison = analyze(client, req["id"])
    assert comparison["evaluation"]["policy_version"] == scheduled["version"]
    assert comparison["evaluation"]["policy_hash"] == scheduled["policy_hash"]
    assert comparison["evaluation"]["current"] and comparison["proposal"] is None
    assert erp.count() == 0
