"""Offline multi-item adapter contracts; no live ERP reliability claim."""
from copy import deepcopy
from decimal import Decimal
import json

import httpx
import pytest

from procureflow.erp import (
    ERPNextClient, ERPRejected, ERPUnknown, MockERP, MULTI_COST_MAPPING_VERSION,
    cost_mapping, cost_proof_matches, expected_mock_cost_values, remote_matches,
)


ACCOUNTS = {"tax": "Synthetic tax", "freight": "Synthetic freight"}


def payload():
    return {"snapshot_hash": "b" * 64, "contract_version": "multi-sku-v1",
        "erp_cost_mapping_version": MULTI_COST_MAPPING_VERSION,
        "erp_company": "Demo", "transaction_date": "2026-10-08", "total": "47.05",
        "erp_cost_accounts": dict(ACCOUNTS),
        "quote_values": {"supplier_id": "SUP-A", "currency": "CNY", "shipping_cost": "5.25", "lines": [
            {"sku": "sku-a", "quantity": "3", "uom": "EA", "unit_price": "7.10",
             "tax_mode": "included", "tax_rate": "0", "discount": "0", "delivery_days": 4},
            {"sku": "SKU-A", "quantity": "2", "uom": "EA", "unit_price": "10.25",
             "tax_mode": "included", "tax_rate": "0", "discount": "0", "delivery_days": 3}]}}


def independent_persisted(body):
    """Persisted arithmetic is a fixed independent fixture, never the expected proof."""
    record = deepcopy(body)
    one_line = len(body["items"]) == 1
    record.update(name="SQ-MULTI", total="20.50" if one_line else "41.80",
        net_total="20.50" if one_line else "41.80", grand_total="25.75" if one_line else "47.05",
        total_taxes_and_charges="5.25")
    by_sku = {"SKU-A": ("20.50", "10.25"), "sku-a": ("21.30", "7.10")}
    for item in record["items"]:
        goods, rate = by_sku[item["item_code"]]
        item.update(amount=goods, net_amount=goods, net_rate=rate)
    record["taxes"][0].update(rate=None, tax_amount="5.25", tax_amount_after_discount_amount="5.25",
        total=record["grand_total"])
    return record


def endpoint(mutation=None, post_status=200):
    calls, state = [], {}
    def handle(request):
        calls.append(request)
        if "/Company/" in request.url.path:
            return httpx.Response(200, json={"data": {"name": "Demo", "default_currency": "CNY"}})
        if request.url.path.endswith("/Custom Field"):
            field = json.loads(request.url.params["filters"])[-1][-1]
            return httpx.Response(200, json={"data": [{"fieldname": field, "unique": 1, "fieldtype": "Data"}]})
        if request.method == "POST":
            state.update(independent_persisted(json.loads(request.content)))
            return httpx.Response(post_status, json={"data": state})
        if request.url.path.endswith("/Supplier Quotation"):
            return httpx.Response(200, json={"data": [{"name": "SQ-MULTI"}] if state else []})
        if request.url.path.endswith("/SQ-MULTI"):
            record = deepcopy(state)
            if mutation:
                mutation(record)
            return httpx.Response(200, json={"data": record})
        pytest.fail(f"Unexpected endpoint: {request.method} {request.url.path}")
    adapter = ERPNextClient("https://erp.test", "synthetic-key", "synthetic-secret", "Demo", True,
        transport=httpx.MockTransport(handle), tax_account=ACCOUNTS["tax"], freight_account=ACCOUNTS["freight"])
    return adapter, calls, state


def test_multi_draft_roundtrip_has_all_case_sensitive_items_and_freight_once():
    adapter, calls, _ = endpoint()
    try:
        result = adapter.create_draft("multi-op", payload())
        assert remote_matches(result, payload(), "multi-op")
        assert result["total"] == "47.05"
        assert [row["sku"] for row in result["lines"]] == ["SKU-A", "sku-a"]
        assert result["multi_cost_proof"]["mapping_version"] == MULTI_COST_MAPPING_VERSION
        assert len(result["multi_cost_proof"]["taxes"]) == 1
        assert len(result["multi_cost_proof"]["lines"]) == 2
        post = next(call for call in calls if call.method == "POST")
        wire = json.loads(post.content, parse_float=Decimal)
        assert wire["items"][0]["rate"] == Decimal("10.25")
        assert wire["items"][1]["qty"] == 3
        assert "delivery_days" in wire["items"][0]["description"]
        assert wire["discount_amount"] == 0
        assert calls[-1].method == "GET"
        assert sum(call.method == "POST" for call in calls) == 1
    finally:
        adapter.client.close()


def test_reordered_persisted_items_use_sku_identity():
    adapter, _, _ = endpoint(mutation=lambda record: record["items"].reverse())
    try:
        assert remote_matches(adapter.create_draft("multi-op", payload()), payload(), "multi-op")
    finally:
        adapter.client.close()


def test_one_line_explicit_multi_contract_roundtrip_and_recovery():
    p = payload(); p["quote_values"]["lines"] = p["quote_values"]["lines"][1:]; p["total"] = "25.75"
    adapter, _, _ = endpoint()
    try:
        record = adapter.create_draft("multi-op", p)
        assert record["cost_proof"]["mapping_version"] != MULTI_COST_MAPPING_VERSION
        assert record["multi_cost_proof"]["mapping_version"] == MULTI_COST_MAPPING_VERSION
        assert remote_matches(record, p, "multi-op")
        assert remote_matches(adapter.find("multi-op"), p, "multi-op")
    finally:
        adapter.client.close()


ROW_MUTATIONS = [
    lambda r: r["items"].pop(),
    lambda r: r["items"].append({**r["items"][0], "item_code": "Unexpected"}),
    lambda r: r["items"].append(deepcopy(r["items"][0])),
    lambda r: r["items"].extend([deepcopy(r["items"][0])] * 19),
    lambda r: r["items"][0].update(item_code="sku-a"),
    lambda r: r["items"][0].update(item_code="SKU-B"),
    lambda r: r["items"][1].update(qty="4"),
    lambda r: r["items"][1].update(rate="7.11"),
    lambda r: r["items"][1].update(uom="BOX"),
    lambda r: r["items"][1].update(price_list_rate="7.11"),
    lambda r: r["items"][1].update(net_rate="7.11"),
    lambda r: r["items"][1].update(discount_amount="1"),
    lambda r: r["items"][1].update(discount_percentage="1"),
    lambda r: r["items"][1].update(item_tax_template="Unexpected template"),
    lambda r: r["items"][1].update(pricing_rules="Unexpected rule"),
    lambda r: r["items"][1].update(description="Changed delivery or tax terms"),
    lambda r: (r["items"][0].update(amount="19.50"), r["items"][1].update(amount="22.30")),
    lambda r: (r["items"][0].update(net_amount="19.50"), r["items"][1].update(net_amount="22.30")),
    lambda r: (r["items"][0].update(rate="9.50"), r["items"][1].update(rate="7.60")),
    lambda r: r["taxes"][0].update(tax_amount_after_discount_amount="4.25"),
    lambda r: r.update(discount_amount="1"),
    lambda r: r.update(net_total="42.80"),
    lambda r: r.update(pricing_rules="Unexpected rule"),
]


@pytest.mark.parametrize("mutation", ROW_MUTATIONS)
def test_every_persisted_item_and_component_checked_despite_correct_post_echo(mutation):
    adapter, calls, _ = endpoint(mutation=mutation)
    try:
        with pytest.raises(ERPRejected):
            adapter.create_draft("multi-op", payload())
        assert sum(call.method == "POST" for call in calls) == 1
        assert calls[-1].method == "GET"
    finally:
        adapter.client.close()


@pytest.mark.parametrize("field,value", [
    ("tax_mode", "unknown"), ("tax_mode", "excluded"), ("tax_rate", "0.13"),
    ("tax_rate", None), ("discount", "0.01"), ("discount", None),
    ("quantity", "2.5"), ("quantity", "NaN"), ("unit_price", "1000000.01"),
    ("unit_price", 7.1), ("unit_price", "Infinity"), ("uom", "BOX"), ("sku", None),
])
def test_unsupported_multi_cost_terms_fail_before_network_and_authority_gate(field, value):
    p = payload(); p["quote_values"]["lines"][0][field] = value
    adapter, calls, _ = endpoint()
    try:
        with pytest.raises(ERPRejected):
            adapter.create_draft_guarded("multi-op", p, lambda: pytest.fail("preflight reached authority gate"))
        assert not calls
    finally:
        adapter.client.close()


@pytest.mark.parametrize("mutation", [
    lambda p: p["quote_values"].update(lines=[]),
    lambda p: p["quote_values"]["lines"].append(deepcopy(p["quote_values"]["lines"][0])),
    lambda p: p["quote_values"]["lines"].extend([{**p["quote_values"]["lines"][0], "sku": f"SKU-{i}"} for i in range(19)]),
    lambda p: p["quote_values"].update(shipping_cost="1000000.01"),
    lambda p: p["quote_values"].update(currency="USD"),
    lambda p: p["quote_values"].update(shipping_cost=None),
    lambda p: p["erp_cost_accounts"].update(freight=""),
    lambda p: p.update(total="47.06"),
    lambda p: p.update(erp_company="Other"),
])
def test_multi_shape_amount_and_target_bounds_fail_before_network(mutation):
    p = payload(); mutation(p)
    adapter, calls, _ = endpoint()
    try:
        with pytest.raises(ERPRejected):
            adapter.create_draft("multi-op", p)
        assert not calls
    finally:
        adapter.client.close()


def test_final_authority_gate_runs_after_metadata_and_prevents_post():
    adapter, calls, _ = endpoint()
    class GateDenied(Exception):
        pass
    def deny():
        assert len(calls) == 3
        assert all(call.method == "GET" for call in calls)
        raise GateDenied()
    try:
        with pytest.raises(GateDenied):
            adapter.create_draft_guarded("multi-op", payload(), deny)
        assert not any(call.method == "POST" for call in calls)
    finally:
        adapter.client.close()


@pytest.mark.parametrize("mutation", [None, lambda r: r["items"][1].update(net_amount="999")])
def test_duplicate_key_reconciliation_reads_every_line(mutation):
    adapter, calls, _ = endpoint(mutation=mutation, post_status=409)
    try:
        if mutation:
            with pytest.raises(ERPRejected):
                adapter.create_draft("multi-op", payload())
        else:
            assert remote_matches(adapter.create_draft("multi-op", payload()), payload(), "multi-op")
        assert sum(call.method == "POST" for call in calls) == 1
    finally:
        adapter.client.close()


def test_uncertain_post_does_not_blindly_retry_and_independent_find_checks_all_lines():
    adapter, calls, state = endpoint(post_status=504)
    try:
        with pytest.raises(ERPUnknown):
            adapter.create_draft("multi-op", payload())
        assert remote_matches(adapter.find("multi-op"), payload(), "multi-op")
        state["items"][1]["net_amount"] = "22.30"
        state["items"][0]["net_amount"] = "19.50"
        assert not remote_matches(adapter.find("multi-op"), payload(), "multi-op")
        assert sum(call.method == "POST" for call in calls) == 1
    finally:
        adapter.client.close()


def test_mock_keeps_full_tax_discount_delivery_components_and_idempotency(tmp_path):
    p = payload(); lines = p["quote_values"]["lines"]
    lines[0].update(discount="1.30", tax_rate="0.13")
    lines[1].update(discount="0.50", tax_mode="excluded", tax_rate="0.13")
    p["total"] = "47.85"
    mock = MockERP(tmp_path / "erp.sqlite3")
    result = mock.create_draft("multi-op", p)
    assert remote_matches(result, p, "multi-op")
    assert result["cost_values"]["calculated"]["goods"] == "41.80"
    assert result["cost_values"]["calculated"]["discount"] == "1.80"
    assert result["cost_values"]["calculated"]["added_tax"] == "2.60"
    assert result["cost_values"]["calculated"]["shipping"] == "5.25"
    assert result["cost_values"] == expected_mock_cost_values(p)
    assert mock.create_draft("multi-op", p) == result and mock.count() == 1
    changed = deepcopy(p); changed["quote_values"]["lines"][0]["delivery_days"] = 5
    with pytest.raises(ERPRejected, match="IDEMPOTENCY_PAYLOAD_CONFLICT"):
        mock.create_draft("multi-op", changed)
    assert mock.count() == 1


@pytest.mark.parametrize("mutation", [
    lambda r: r["lines"].pop(),
    lambda r: r["lines"].append(deepcopy(r["lines"][0])),
    lambda r: r["lines"].append({**r["lines"][0], "sku": "unexpected"}),
    lambda r: r["lines"][1].update(unit_price="7.11"),
    lambda r: r["lines"][1].update(quantity=True),
    lambda r: r["lines"][1].update(sku="SKU-A"),
    lambda r: r["cost_values"]["lines"][1].update(delivery_days=99),
    lambda r: r["cost_values"]["lines"][1].update(discount="0.01"),
    lambda r: r["cost_values"]["lines"][1].update(tax_rate="0.13"),
    lambda r: r["cost_values"]["calculated"]["lines"][1].update(goods="21.50"),
    lambda r: r["cost_values"].update(shipping_cost="5.24"),
])
def test_mock_readback_detects_every_line_and_component_mutation(tmp_path, mutation):
    p = payload(); mock = MockERP(tmp_path / "erp.sqlite3")
    remote = mock.create_draft("multi-op", p); mutation(remote)
    assert not remote_matches(remote, p, "multi-op")


def test_mock_lost_response_reconciles_without_duplicate_and_reordered_identity_is_allowed(tmp_path):
    mock = MockERP(tmp_path / "erp.sqlite3"); p = payload()
    mock.fail_after_commit_once.add("multi-op")
    with pytest.raises(ERPUnknown):
        mock.create_draft("multi-op", p)
    remote = mock.find("multi-op"); remote["lines"].reverse()
    assert remote_matches(remote, p, "multi-op")
    assert mock.create_draft("multi-op", p)["name"] == remote["name"]
    assert mock.count() == 1


@pytest.mark.parametrize("field", ["quantity", "unit_price", "item_net_total", "tax_amount"])
@pytest.mark.parametrize("value", [True, "NaN", "Infinity", None])
def test_component_comparison_rejects_invalid_numeric_values(field, value):
    assert not cost_proof_matches({"lines": [{field: value}]}, {"lines": [{field: "1"}]})


@pytest.mark.parametrize("mode", ["included", "excluded"])
def test_zero_freight_and_maximum_twenty_item_mapping_has_no_implicit_rows(mode):
    p = payload(); p["quote_values"]["shipping_cost"] = "0"; p["erp_cost_accounts"] = {"tax": "", "freight": ""}
    p["quote_values"]["lines"] = [{"sku": f"sku-{i:02}", "quantity": "2", "uom": "EA", "unit_price": "10.25",
        "tax_mode": mode, "tax_rate": "0", "discount": "0", "delivery_days": 4} for i in range(20)]
    p["total"] = "410.00"
    body, proof = cost_mapping(p)
    assert body["taxes"] == proof["taxes"] == []
    assert body["apply_discount_on"] == ("Grand Total" if mode == "included" else "Net Total")
    assert len(proof["lines"]) == 20
    assert Decimal(proof["goods_total"]) == Decimal(proof["net_total"]) == Decimal("410.00")
    assert Decimal(proof["total_taxes_and_charges"]) == 0


def test_aggregate_limit_checks_all_lines_not_only_first_item():
    p = payload(); p["quote_values"]["shipping_cost"] = "0"
    for line in p["quote_values"]["lines"]:
        line.update(quantity="1", unit_price="500000.01")
    p["total"] = "1000000.02"
    adapter, calls, _ = endpoint()
    try:
        with pytest.raises(ERPRejected, match="BOUNDS_EXCEEDED"):
            adapter.create_draft("multi-op", p)
        assert not calls
    finally:
        adapter.client.close()


@pytest.mark.parametrize("field,value", [("qty", "NaN"), ("rate", "Infinity"), ("qty", True), ("rate", None)])
def test_malformed_nonfirst_item_remains_uncertain(field, value):
    adapter, calls, _ = endpoint(mutation=lambda r: r["items"][1].update({field: value}))
    try:
        with pytest.raises(ERPUnknown, match="MALFORMED_AMOUNTS"):
            adapter.create_draft("multi-op", payload())
        assert sum(call.method == "POST" for call in calls) == 1
    finally:
        adapter.client.close()


def test_legacy_scalar_payload_cannot_accept_same_first_item_with_extra_rows():
    from test_erp_cost_mapping import endpoint as single_endpoint, payload as single_payload
    adapter, _, _ = single_endpoint()
    try:
        p = single_payload(); remote = adapter.create_draft("single-op", p)
        assert remote_matches(remote, p, "single-op")
        remote["lines"].append({**remote["lines"][0], "sku": "EXTRA"})
        assert not remote_matches(remote, p, "single-op")
    finally:
        adapter.client.close()
