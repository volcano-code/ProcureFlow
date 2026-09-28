"""Opt-in integration probes, never procurement writes.

Without --allow-network this exits blocked before constructing any HTTP client.
The model probe sends fixed synthetic data only and can incur provider charges.
ERP probe performs GET-only identity/company/field checks. Neither probe reads
business databases, grants approval, creates an ERP draft, or claims MVP success.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))


def run_probe(target: str, allow_network: bool) -> tuple[dict, int]:
    report = {"scope": "synthetic model compatibility" if target == "model" else "ERPNext GET-only preflight",
              "status": "blocked", "target": target, "network_attempted": False,
              "external_business_writes": 0, "live_draft_roundtrip_verified": False,
              "semantic_factuality_verified": False, "cost": None}
    if not allow_network:
        return {**report, "reason": "EXPLICIT_NETWORK_OPT_IN_REQUIRED"}, 2
    if os.getenv("PF_MODE") != "private" or os.getenv("PF_INTEGRATION_ENVIRONMENT") != "sandbox":
        return {**report, "reason": "PRIVATE_SANDBOX_CONFIGURATION_REQUIRED"}, 2
    required = ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL") if target == "model" else (
        "ERP_BASE_URL", "ERP_API_KEY", "ERP_API_SECRET", "ERP_COMPANY", "ERP_EXPECTED_INTEGRATION_USER")
    if not all(os.getenv(key) for key in required) or not os.getenv("PF_AUTH_TOKENS"):
        return {**report, "reason": "INTEGRATION_CONFIGURATION_REQUIRED"}, 2
    from procureflow.config import Settings
    from procureflow.agent import ReadOnlyAgent
    from procureflow.erp import ERPNextClient, ERPRejected, ERPUnknown
    from procureflow.errors import DomainError
    try:
        with tempfile.TemporaryDirectory(prefix="pf-probe-") as tmp:
            # Do not instantiate create_app or open PF_DATABASE_URL.
            settings = Settings(data_dir=Path(tmp), database_url="sqlite:///:memory:",
                                erp_allow_draft_writes=False)
            if target == "erp":
                if settings.erp_mode != "erpnext":
                    return {**report, "reason": "ERPNEXT_MODE_REQUIRED"}, 2
                expected_user = os.environ["ERP_EXPECTED_INTEGRATION_USER"]
                if expected_user.casefold() in {"administrator", "guest"}:
                    return {**report, "reason": "DEDICATED_INTEGRATION_IDENTITY_REQUIRED"}, 2
                client = ERPNextClient(settings.erp_url, settings.erp_api_key, settings.erp_api_secret,
                                       settings.erp_company, allow_writes=False)
                try:
                    report["network_attempted"] = True
                    report["checks"] = client.preflight(expected_user=expected_user)
                    report["status"] = "read_only_checks_passed"
                finally:
                    client.client.close()
            else:
                # No user quotes, policies, account data or secrets enter the model messages.
                fixtures = {
                    "get_comparison": {"synthetic": True, "quotes": [{"document_id": "probe-doc",
                        "supplier_id": "SYNTHETIC-A", "quantity": "2", "unit_price": "100.00",
                        "shipping_cost": None, "total": None, "eligible": False,
                        "reason": "shipping unknown", "evidence_ids": ["probe:price"]}]},
                    "search_policy": {"synthetic": True, "rule": "Unknown shipping must remain unknown; no purchase is authorized."},
                    "get_evidence": {"synthetic": True, "fragment_id": "probe:price", "text": "unit_price: 100.00; quantity: 2; shipping: unknown"}}
                invoked = []
                def invoke(name, arguments):
                    if name == "get_evidence" and arguments.get("document_id") != "probe-doc":
                        raise DomainError("TOOL_SCOPE_DENIED", "Only synthetic evidence is available", 403)
                    invoked.append(name)
                    return fixtures[name]
                agent = ReadOnlyAgent(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"], os.environ["LLM_MODEL"],
                    thinking_mode=os.getenv("LLM_THINKING_MODE", "default"), require_usage=True,
                    required_tools=("get_comparison", "search_policy"))
                try:
                    report["network_attempted"] = True
                    result = agent.run(invoke, {"probe:price"})
                    # Do not persist provider prose, private reasoning, source content or arbitrary metadata.
                    report.update(status="compatibility_checks_passed", synthetic_input_only=True,
                        advisory_only=result["advisory_only"], model_calls=result["model_calls"],
                        tool_calls=result["tool_calls"], tools_invoked=invoked,
                        usage=result["usage"], usage_source="provider_reported",
                        evidence_ids_valid=True, task_success_rate=None)
                finally:
                    agent.client.close()
        return report, 0
    except (DomainError, ERPRejected, ERPUnknown, ValueError, TypeError, AttributeError) as error:
        code = error.code if isinstance(error, DomainError) else str(error) if isinstance(error, (ERPRejected, ERPUnknown)) else "INVALID_INTEGRATION_CONFIGURATION"
        report.update(status="failed", reason=code if re.fullmatch(r"[A-Z0-9_]+", code) else "INTEGRATION_PROBE_FAILED")
        return report, 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("model", "erp"), required=True)
    parser.add_argument("--allow-network", action="store_true", help="Authorize this synthetic/read-only probe; model calls may cost money")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report, code = run_probe(args.target, args.allow_network)
    report["exit_code"] = code
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
