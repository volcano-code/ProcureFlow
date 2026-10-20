"""Actual native browser -> PostgreSQL -> independent candidate container gate.

Synthetic fixture, not real model quality/ERP acceptance. Opt-in and a dedicated
pf-candidate-* Compose project are mandatory. No HTTP interception/route mocks.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from check_candidate_stack import STEPS, check, require
from fixtures.candidate_provider import KEY, MODEL, TEXT

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:3000"
FILES = ["compose.postgres.yaml", "compose.web.yaml",
         "compose.candidate-worker.yaml", "compose.candidate-fixture.yaml"]
BUYER = {"Authorization": "Bearer demo-buyer"}

PROBE = """
import json, os
from sqlalchemy import select, func
from procureflow.config import Settings
from procureflow.db import Database, QuoteCandidateRunRow, QuoteRow, QuoteVersionRow, ApprovalRow, OperationRow
s=Settings()
assert s.mode == 'demo' and s.erp_mode == 'mock' and not s.erp_allow_draft_writes
db=Database(s.database_url, create_schema=False)
assert db.engine.dialect.name == 'postgresql'
db.check_ready(require_migrations=True)
with db.transaction() as session:
    rows=session.scalars(select(QuoteCandidateRunRow).order_by(QuoteCandidateRunRow.created_at)).all()
    counts={name:session.scalar(select(func.count()).select_from(model)) for name,model in
        [('candidate_runs',QuoteCandidateRunRow),('quotes',QuoteRow),('approvals',ApprovalRow),('external_operations',OperationRow)]}
    counts['confirmed_quotes']=session.scalar(select(func.count()).select_from(QuoteVersionRow).where(QuoteVersionRow.confirmed_by.is_not(None)))
    counts['candidate_outputs']=sum(row.output is not None for row in rows)
    print(json.dumps({'database':'postgresql','counts':counts,
        'api_without_provider_key':not bool(os.getenv('LLM_API_KEY')),
        'runs':[{'id':r.id,'status':r.status,'has_output':r.output is not None} for r in rows]}))
db.engine.dispose()
if db.fence_engine is not None:db.fence_engine.dispose()
"""


def compose(*args, input_text=None, timeout=90):
    prefix = ["docker", "compose", "--env-file", ".env.postgres"]
    for name in FILES:
        prefix += ["-f", name]
    result = subprocess.run(prefix + list(args), cwd=ROOT, input=input_text,
        capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("COMPOSE_COMMAND_FAILED")
    return result.stdout


def api_probe():
    return json.loads(compose("exec", "-T", "api", "python", "-", input_text=PROBE))


def provider_control(action=None):
    code = f"""
import json,urllib.request
headers={{'Authorization':'Bearer '+{KEY!r},'Content-Type':'application/json'}}
action={action!r}
r=urllib.request.Request('http://127.0.0.1:19090/'+('control' if action else 'state'),
    data=json.dumps({{'action':action}}).encode() if action else None,headers=headers)
with urllib.request.urlopen(r,timeout=3) as response: print(response.read().decode())
"""
    return json.loads(compose("exec", "-T", "candidate-fixture", "python", "-", input_text=code))


def wait_for(predicate, code, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError(code)


def expire_killed_run(run_id):
    require(re.fullmatch(r"can_[0-9a-f]{32}", run_id) is not None, "INVALID_RUN_ID")
    # Only the already-guarded disposable PG stack. Inject expiry, not wall-clock evidence.
    code = PROBE.split("with db.transaction()")[0] + f"""
with db.transaction(write=True) as session:
    row=session.get(QuoteCandidateRunRow,{run_id!r})
    assert row is not None and row.status=='RUNNING' and row.output is None
    row.lease_until='2000-01-01T00:00:00+00:00'
print('{{"lease_expiry_injected":true}}')
"""
    compose("exec", "-T", "api", "python", "-", input_text=code)


def environment_guard():
    require(os.getenv("PF_CANDIDATE_STACK_FIXTURE") == "1", "FIXTURE_OPT_IN_REQUIRED")
    require(re.fullmatch(r"pf-candidate-[a-z0-9-]{1,60}",
        os.getenv("COMPOSE_PROJECT_NAME", "")) is not None, "DISPOSABLE_PROJECT_REQUIRED")
    require(os.getenv("PF_WEB_PORT", "3000") == "3000", "FIXED_LOOPBACK_PORT_REQUIRED")
    # No docker mutation before these guards. Services must already be started by the operator.
    before = api_probe()
    require(before["api_without_provider_key"], "API_HAS_PROVIDER_KEY")
    require(all(v == 0 for v in before["counts"].values()), "FRESH_DATABASE_REQUIRED")
    require(provider_control() == {"scripted": True, "calls": 0, "waiting": 0, "hold": False},
            "FRESH_PROVIDER_REQUIRED")
    # Inspect only fixed booleans, never dump the container environment.
    worker_guard = f"""
import json,os
assert os.getenv('LLM_API_KEY')=={KEY!r}
assert os.getenv('LLM_MODEL')=={MODEL!r}
assert os.getenv('LLM_BASE_URL')=='http://127.0.0.1:19090'
assert os.getenv('PF_ALLOW_CANDIDATE_MODEL')=='1'
assert os.getenv('PF_CANDIDATE_STACK_FIXTURE')=='1'
print('{{"synthetic_worker_configuration":true}}')
"""
    compose("exec", "-T", "candidate-worker", "python", "-", input_text=worker_guard)
    return before


def exercise(output_dir):
    from playwright.sync_api import sync_playwright, expect
    report = {"schema": "candidate-stack-fixture-v1", "status": "running", "steps": [],
        "runs": [], "synthetic_only": True, "real_model_used": False, "real_erp_used": False,
        "model_quality_measured": False, "browser_responses_not_mocked": True,
        "real_sigkill": False, "lease_expiry_injected": False, "no_automatic_replay": False,
        "source_commit": subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()}
    phase = "scope"
    page = None
    try:
        environment_guard()
        report.update(database="postgresql", api_without_provider_key=True)
        compose("stop", "candidate-worker")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width":1440,"height":1100})
            page_errors = []
            requests = []
            page.on("pageerror", lambda error: page_errors.append(type(error).__name__))
            page.on("request", lambda r: requests.append((r.method, r.url))
                    if "/api/v1/" in r.url else None)
            page.goto(URL)
            page.get_by_test_id("login-demo-buyer").click()
            expect(page.get_by_test_id("load-demo")).to_be_enabled(timeout=15000)
            cap = page.request.get(URL + "/backend/api/v1/capabilities", headers=BUYER).json()
            require(cap["candidate_queue_enabled"] and not cap["candidate_model_configured"]
                    and cap["mode"] == "demo" and cap["erp_mode"] == "mock", "CAPABILITY_MISMATCH")
            report["steps"].append(STEPS[0])

            def call(method, path, body=None, token="demo-buyer", expected=200):
                response = page.request.fetch(URL + "/backend/api/v1" + path, method=method,
                    headers={"Authorization":"Bearer "+token}, data=body)
                require(response.status == expected, "UNEXPECTED_API_RESPONSE")
                return response.json()

            def create(scenario):
                nonlocal phase
                phase = scenario + "-create"
                page.get_by_role("button", name="＋ 新建采购需求").click()
                form = page.get_by_test_id("request-form")
                form.locator('[name="title"]').fill("Candidate stack " + scenario)
                form.locator('[name="sku"]').fill("STAND-01")
                with page.expect_response(lambda r: r.request.method == "POST" and
                        r.url.endswith("/api/v1/requests")) as response:
                    form.get_by_role("button", name="保存需求").click()
                require(response.value.status == 201, "REQUEST_CREATE_FAILED")
                request = response.value.json()
                expect(form).to_have_count(0)
                # The modal closes before the new request's asynchronous refresh.
                # Wait for its actual header and enabled upload control, not the
                # previous request's still-visible hidden input.
                phase = scenario + "-upload"
                expect(page.locator(".request-panel").get_by_role("heading",
                    name="Candidate stack " + scenario, exact=True)).to_be_visible()
                panel = page.get_by_test_id("candidate-panel")
                upload = panel.get_by_role("button", name="上传候选原文", exact=True)
                expect(upload).to_be_enabled()
                with page.expect_file_chooser() as chooser:
                    upload.click()
                with page.expect_response(lambda r: r.request.method == "POST" and
                        r.url.endswith(f"/requests/{request['id']}/candidate-runs")) as response:
                    chooser.value.set_files({
                        "name":scenario+".txt","mimeType":"text/plain",
                        "buffer":(TEXT+"\nfixture-case: "+scenario).encode()})
                require(response.value.status == 201, "RUN_RESERVE_FAILED")
                run = response.value.json()
                card = page.get_by_test_id("candidate-run").filter(has_text=run["id"])
                expect(card).to_contain_text("已预留，未调用模型")
                return request, run, card

            def enqueue_ui(card):
                panel = page.get_by_test_id("candidate-panel")
                panel.get_by_label("我允许将本任务原文发送给服务端配置的模型；可能产生费用").check()
                with page.expect_response(lambda r: r.request.method == "POST" and
                        r.url.endswith("/enqueue")) as response:
                    card.get_by_role("button", name="开始候选提取").click()
                require(response.value.status == 202, "ENQUEUE_FAILED")

            phase = "browser-reserve"
            request, normal, card = create("normal")
            require(provider_control()["calls"] == 0, "API_CALLED_PROVIDER")
            report["steps"].append(STEPS[1])
            enqueue_ui(card)
            expect(card).to_contain_text("已排队")
            require(provider_control()["calls"] == 0, "QUEUED_CALLED_PROVIDER")
            phase = "restart"
            compose("restart", "api", "web")
            # Up only the API/web dependencies: do not accidentally start the stopped Worker.
            compose("up", "-d", "--wait", "--wait-timeout", "90", "web", timeout=120)
            page.reload()
            page.get_by_test_id("login-demo-buyer").click()
            expect(page.get_by_test_id("candidate-run").filter(has_text=normal["id"])).to_contain_text("已排队",timeout=15000)
            require(provider_control()["calls"] == 0, "RESTART_CALLED_PROVIDER")
            report["steps"].append(STEPS[2])
            phase = "worker"
            compose("start", "candidate-worker")
            card = page.get_by_test_id("candidate-run").filter(has_text=normal["id"])
            expect(card).to_contain_text("等待人工核对",timeout=45000)
            require(provider_control()["calls"] == 2, "NORMAL_PROVIDER_COUNT")
            report["steps"].append(STEPS[3])
            phase = "human-import"
            card.get_by_label("我理解这不是已确认事实，后续仍须逐字段核对").check()
            card.get_by_label("候选采纳理由").fill("Synthetic integration test; retain independent confirmation.")
            card.get_by_role("button", name="导入为未确认报价").click()
            expect(page.get_by_test_id("quote-row")).to_have_count(1)
            require(call("GET",f"/requests/{request['id']}/quotes")[0]["confirmed_by"] is None, "AUTO_CONFIRMED")
            page.screenshot(path=str(output_dir/"candidate-desktop.png"), full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            require(page.evaluate("document.documentElement.scrollWidth <= innerWidth"),"MOBILE_OVERFLOW")
            page.screenshot(path=str(output_dir/"candidate-mobile.png"), full_page=True)
            page.set_viewport_size({"width":1440,"height":1100})
            report["runs"].append({"scenario":"normal","id":normal["id"],"status":"IMPORTED"})
            report["steps"].append(STEPS[4])
            replay = call("POST",f"/candidate-runs/{normal['id']}/enqueue",
                {"expected_revision":normal["revision"],"acknowledge_external_model":True},expected=202)
            require(replay["status"] == "IMPORTED" and provider_control()["calls"] == 2, "TERMINAL_REPLAY")
            report["steps"].append(STEPS[5])

            phase = "cancel-queued"
            compose("stop", "candidate-worker")
            _, queued, card = create("queued_cancel")
            enqueue_ui(card)
            expect(card).to_contain_text("已排队")
            card.get_by_role("button",name="取消候选任务").click()
            expect(card).to_contain_text("已取消")
            compose("start", "candidate-worker")
            # Run a complete independent drain after cancellation; no timing-only inference.
            compose("exec","-T","candidate-worker","python","-m","procureflow.candidate_worker","--once")
            require(provider_control()["calls"] == 2, "CANCELLED_CALLED_PROVIDER")
            report["runs"].append({"scenario":"queued_cancel","id":queued["id"],"status":"CANCELLED"})
            report["steps"].append(STEPS[6])

            phase = "cancel-inflight"
            provider_control("hold")
            _, inflight, card = create("inflight_cancel")
            enqueue_ui(card)
            wait_for(lambda: provider_control()["waiting"] == 1, "PROVIDER_NOT_ENTERED")
            expect(card).to_contain_text("提取中",timeout=15000)
            card.get_by_role("button",name="取消候选任务").click()
            expect(card).to_contain_text("已取消")
            provider_control("release")
            wait_for(lambda: provider_control()["waiting"] == 0, "PROVIDER_NOT_RELEASED")
            compose("stop","candidate-worker")
            result = call("GET",f"/candidate-runs/{inflight['id']}")
            require(result["status"] == "CANCELLED" and result["output"] is None
                    and provider_control()["calls"] == 3,"LATE_OUTPUT_ACCEPTED")
            report["runs"].append({"scenario":"inflight_cancel","id":inflight['id'],"status":"CANCELLED"})
            report["steps"].append(STEPS[7])

            phase = "sigkill"
            provider_control("hold")
            _, killed, card = create("killed")
            enqueue_ui(card)
            compose("start","candidate-worker")
            wait_for(lambda: provider_control()["waiting"] == 1,"PROVIDER_NOT_ENTERED")
            require(call("GET",f"/candidate-runs/{killed['id']}")["stored_status"] == "RUNNING","NOT_RUNNING")
            compose("kill","-s","SIGKILL","candidate-worker")
            container_id = compose("ps","-a","-q","candidate-worker").strip()
            inspected = subprocess.run(["docker","inspect","--format","{{.State.ExitCode}}",container_id],
                capture_output=True,text=True,check=True,timeout=10)
            require(inspected.stdout.strip() == "137", "KILL_NOT_OBSERVED")
            report["real_sigkill"] = True
            expire_killed_run(killed["id"])
            report["lease_expiry_injected"] = True
            provider_control("release")
            wait_for(lambda: provider_control()["waiting"] == 0,"PROVIDER_NOT_RELEASED")
            compose("start","candidate-worker")
            wait_for(lambda: next((r["status"]=="INTERRUPTED" for r in api_probe()["runs"]
                if r["id"]==killed["id"]),False),"INTERRUPTION_NOT_PERSISTED")
            compose("exec","-T","candidate-worker","python","-m","procureflow.candidate_worker","--once")
            require(provider_control()["calls"]==4,"UNCERTAIN_CALL_REPLAYED")
            result=call("GET",f"/candidate-runs/{killed['id']}")
            require(result["output"] is None and result["stored_status"]=="INTERRUPTED","KILLED_OUTPUT_ACCEPTED")
            report["no_automatic_replay"] = True
            report["runs"].append({"scenario":"killed","id":killed["id"],"status":"INTERRUPTED"})
            report["steps"].append(STEPS[8])

            phase = "audit"
            call("GET",f"/candidate-runs/{normal['id']}",token="demo-other-tenant",expected=404)
            call("POST",f"/candidate-runs/{killed['id']}/enqueue",
                {"expected_revision":result["revision"],"acknowledge_external_model":True},
                token="demo-approver",expected=403)
            db = api_probe()
            require(not page_errors,"BROWSER_PAGE_ERROR")
            require(not any(method=="POST" and url.endswith(("/process","/approval","/execute"))
                            for method,url in requests),"UNEXPECTED_BROWSER_WRITE_PATH")
            require(all(next(r["status"] for r in db["runs"] if r["id"]==item["id"])==item["status"]
                        for item in report["runs"]),"RECEIPT_DB_MISMATCH")
            report.update(provider_requests=provider_control()["calls"],database_counts=db["counts"])
            report["steps"].append(STEPS[9])
            report["status"]="passed"
            check(report)
            browser.close()
    except Exception as error:
        # Never publish exception text, request bodies, credentials or provider content.
        report.update(status="failed",phase=phase,error_type=type(error).__name__,
                      reason="CANDIDATE_STACK_GATE_FAILED")
        if page is not None:
            try:page.screenshot(path=str(output_dir/"failure.png"),full_page=True)
            except Exception:pass
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Do not overwrite an earlier attempt, passed or failed.
    with args.output.open("x",encoding="utf-8") as output:
        result={"status":"blocked","network_attempted":False,"reason":"EXPLICIT_FIXTURE_OPT_IN_REQUIRED"}
        if args.fixture:
            result=exercise(args.output.parent)
        json.dump(result,output,ensure_ascii=False,indent=2)
    print(json.dumps({"status":result["status"],"steps_completed":len(result.get("steps",[]))}))
    return 0 if result["status"]=="passed" else 2 if result["status"]=="blocked" else 1


if __name__=="__main__":
    raise SystemExit(main())
