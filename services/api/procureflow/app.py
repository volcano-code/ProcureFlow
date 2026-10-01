from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy.exc import SQLAlchemyError
from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from . import __version__
from .agent import LANGGRAPH_VERSION, RUNTIME, ReadOnlyAgent
from .advice import AdviceService
from .config import Settings
from .contracts import (AdviceRunCommand, AnalyzeCommand, ApprovalCommand, ExecuteCommand, Principal,
    QuoteConfirm, QuoteEdit, RequestCreate, RequestUpdate)
from .db import Database, audit
from .domain import POLICY
from .erp import ERPNextClient, ERPRejected, ERPUnknown, MockERP
from .errors import DomainError
from .service import ProcurementService, require

ROOT = Path(__file__).resolve().parents[3]


def create_app(settings: Settings | None = None, database: Database | None = None, erp=None) -> FastAPI:
    settings = settings or Settings()
    db = database or Database(settings.database_url,
        create_schema=settings.mode == "demo" and settings.database_url.startswith("sqlite"))
    if erp is None:
        erp = MockERP(settings.data_dir / "mock-erp.sqlite3") if settings.erp_mode == "mock" else ERPNextClient(
            settings.erp_url, settings.erp_api_key, settings.erp_api_secret, settings.erp_company, settings.erp_allow_draft_writes)
    service = ProcurementService(db, settings, erp)
    advice_service = AdviceService(service)

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.engine.dispose()
        if hasattr(erp, "client"):
            erp.client.close()

    app = FastAPI(title="ProcureFlow", version=__version__, lifespan=lifespan,
        description="Evidence-first procurement local alpha. Mock ERP and deterministic baseline by default; no live ordering.")
    app.state.service, app.state.settings = service, settings
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.web_origins),
                       allow_credentials=False, allow_methods=["GET", "POST", "PUT"],
                       allow_headers=["Authorization", "Content-Type", "Last-Event-ID"])
    bearer = HTTPBearer(auto_error=False)

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise DomainError("UNAUTHENTICATED", "A bearer token is required", 401)
        # No client-supplied tenant, role or approver ID is trusted.
        for token, principal in settings.auth_tokens.items():
            if secrets.compare_digest(token.encode(), credentials.credentials.encode()):
                return Principal.model_validate(principal)
        raise DomainError("UNAUTHENTICATED", "Invalid bearer token", 401)

    @app.exception_handler(DomainError)
    async def domain_error(_, error):
        return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": error.message}})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > settings.max_upload_bytes + 65536:
                    return JSONResponse(status_code=413, content={"error": {"code": "BODY_LIMIT", "message": "Request body too large"}})
            except ValueError:
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_LENGTH", "message": "Invalid content length"}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Streaming audit events must not be buffered by a proxy compressor.
        is_event_stream = response.headers.get("content-type", "").startswith("text/event-stream")
        response.headers["Cache-Control"] = "no-store, no-transform" if is_event_stream else "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "mode": settings.mode, "erp": erp.mode,
                "analysis_default": "deterministic-baseline", "llm_live_verified": False,
                "database": "sqlite" if db.sqlite else "postgresql", "public_production_ready": False}

    @app.get("/ready")
    def readiness():
        try:
            return db.check_ready(require_migrations=not (db.sqlite and settings.mode == "demo"))
        except (SQLAlchemyError, RuntimeError):
            # No raw DSN, SQL, credentials or exception details in unauthenticated output.
            return JSONResponse(status_code=503, content={"status": "not_ready",
                "error": "DATABASE_NOT_READY"})

    @app.get("/api/v1/capabilities")
    def capabilities(principal=Depends(identity)):
        return {"mode": settings.mode, "erp_mode": erp.mode,
                "demo_samples": settings.mode == "demo" and erp.mode == "mock",
                "advice_configured": bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL")),
                "advice_runtime": RUNTIME, "advice_runtime_version": LANGGRAPH_VERSION,
                "erp_draft_writes_enabled": erp.mode == "mock" or settings.erp_allow_draft_writes,
                "approval_authority": "business-database", "production_ready": False}

    def require_demo(principal):
        require(principal, "buyer")
        if settings.mode != "demo" or erp.mode != "mock":
            raise DomainError("DEMO_DISABLED", "Synthetic demo loading is disabled for this environment", 404)

    @app.get("/api/v1/demo/samples")
    def demo_samples(principal=Depends(identity)):
        require_demo(principal)
        return {"synthetic": True, "request": {"title": "研发工位支架采购（合成演示）",
            "sku": "STAND-01", "quantity": "20", "budget": "30000.00",
            "max_delivery_days": 14, "uom": "EA", "currency": "CNY"},
            "files": ["supplier-a.txt", "supplier-b.csv", "supplier-c.pdf"]}

    @app.get("/api/v1/demo/samples/{name}")
    def demo_sample(name: str, principal=Depends(identity)):
        require_demo(principal)
        if name not in {"supplier-a.txt", "supplier-b.csv", "supplier-c.pdf"}:
            raise DomainError("NOT_FOUND", "Unknown synthetic sample", 404)
        path = ROOT / "apps" / "demo" / "samples" / name
        if not path.is_file():
            raise DomainError("SAMPLE_MISSING", "The packaged synthetic sample is missing", 503)
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.get("/api/v1/me")
    def me(principal=Depends(identity)):
        return principal

    @app.get("/api/v1/requests")
    def list_requests(principal=Depends(identity)):
        return service.list_requests(principal)

    @app.post("/api/v1/requests", status_code=201)
    def create_request(command: RequestCreate, principal=Depends(identity)):
        return service.create_request(principal, command)

    @app.get("/api/v1/requests/{request_id}")
    def get_request(request_id: str, principal=Depends(identity)):
        return service.get_request(principal, request_id)

    @app.put("/api/v1/requests/{request_id}")
    def update_request(request_id: str, command: RequestUpdate, principal=Depends(identity)):
        return service.update_request(principal, request_id, command)

    @app.post("/api/v1/requests/{request_id}/documents", status_code=201)
    async def upload(request_id: str, file: UploadFile = File(...), principal=Depends(identity)):
        # Upload byte cap also applies without Content-Length. Parser sandboxing is not implemented.
        require(principal, "buyer")
        data = await file.read(settings.max_upload_bytes + 1)
        await file.close()
        return await asyncio.to_thread(service.import_quote, principal, request_id, file.filename or "upload.txt", data)

    @app.get("/api/v1/requests/{request_id}/quotes")
    def quotes(request_id: str, principal=Depends(identity)):
        return service.list_quotes(principal, request_id)

    @app.put("/api/v1/quotes/{quote_id}")
    def edit_quote(quote_id: str, command: QuoteEdit, principal=Depends(identity)):
        return service.edit_quote(principal, quote_id, command)

    @app.post("/api/v1/quotes/{quote_id}/confirm")
    def confirm_quote(quote_id: str, command: QuoteConfirm, principal=Depends(identity)):
        return service.confirm_quote(principal, quote_id, command)

    @app.get("/api/v1/quotes/{quote_id}/versions")
    def quote_history(quote_id: str, principal=Depends(identity)):
        return service.quote_history(principal, quote_id)

    @app.get("/api/v1/documents/{document_id}/evidence")
    def evidence(document_id: str, principal=Depends(identity)):
        return service.evidence(principal, document_id)

    @app.get("/api/v1/policy")
    def policy(principal=Depends(identity)):
        return POLICY

    @app.get("/api/v1/suppliers")
    def suppliers(principal=Depends(identity)):
        try:
            return {"mode": erp.mode, "items": erp.suppliers(), "limit": 100, "may_have_more": erp.mode == "erpnext"}
        except (ERPRejected, ERPUnknown) as error:
            raise DomainError("ERP_READ_FAILED", "Supplier lookup failed; check server-side ERP configuration", 502) from error

    @app.post("/api/v1/requests/{request_id}/analyze")
    def analyze(request_id: str, command: AnalyzeCommand, principal=Depends(identity)):
        return service.analyze(principal, request_id, command.preferred_quote_id)

    @app.post("/api/v1/requests/{request_id}/advice")
    def advice(request_id: str, principal=Depends(identity)):
        require(principal, "buyer")
        request = service.get_request(principal, request_id)
        quotes = service.list_quotes(principal, request_id)
        docs = {q["document_id"] for q in quotes}
        ids = {e["fragment_id"] for q in quotes for e in q["evidence"].values() if e.get("fragment_id")}
        def invoke(name, arguments):
            if name == "get_comparison":
                return {"request": request, "quotes": quotes}
            if name == "search_policy":
                return POLICY
            if name == "get_evidence" and arguments["document_id"] in docs:
                return service.evidence(principal, arguments["document_id"])
            raise DomainError("TOOL_SCOPE_DENIED", "Document is outside this agent request scope", 403)
        agent = ReadOnlyAgent.from_env()
        try:
            def observe(event):
                with db.transaction(write=True) as session:
                    audit(session, principal, request_id, "AGENT_" + event["type"].upper(), event)
            output = agent.run(invoke, ids, observer=observe)
        except DomainError as error:
            with db.transaction(write=True) as session:
                audit(session, principal, request_id, "AGENT_ADVICE_FAILED", {"error_code": error.code})
            raise
        finally:
            agent.client.close()
        with db.transaction(write=True) as session:
            audit(session, principal, request_id, "AGENT_ADVICE_COMPLETED", {
                "model_calls": output["model_calls"], "tool_calls": output["tool_calls"],
                "advisory_only": True, "trace": output["trace"]})
        return output

    @app.post("/api/v1/requests/{request_id}/advice-runs", status_code=201)
    def reserve_advice(request_id: str, command: AdviceRunCommand, principal=Depends(identity)):
        return advice_service.reserve(principal, request_id, command)

    @app.get("/api/v1/requests/{request_id}/advice-runs")
    def list_advice(request_id: str, principal=Depends(identity)):
        return advice_service.list(principal, request_id)

    @app.get("/api/v1/advice-runs/{run_id}")
    def get_advice(run_id: str, principal=Depends(identity)):
        return advice_service.get(principal, run_id)

    @app.post("/api/v1/advice-runs/{run_id}/process")
    def process_advice(run_id: str, principal=Depends(identity)):
        return advice_service.process(principal, run_id, ReadOnlyAgent.from_env)

    @app.post("/api/v1/requests/{request_id}/approval")
    def approve(request_id: str, command: ApprovalCommand, principal=Depends(identity)):
        return service.approve(principal, request_id, command)

    @app.post("/api/v1/requests/{request_id}/execute", status_code=202)
    def execute(request_id: str, command: ExecuteCommand, principal=Depends(identity)):
        return service.enqueue(principal, request_id, command.snapshot_hash)

    @app.get("/api/v1/operations/{operation_id}")
    def operation(operation_id: str, principal=Depends(identity)):
        return service.get_operation(principal, operation_id)

    @app.post("/api/v1/operations/{operation_id}/verify")
    def verify_operation(operation_id: str, principal=Depends(identity)):
        return service.verify_operation(principal, operation_id)

    @app.post("/api/v1/operations/{operation_id}/process")
    def process(operation_id: str, principal=Depends(identity)):
        return service.process_operation(principal, operation_id)

    @app.get("/api/v1/requests/{request_id}/events")
    def events(request_id: str, after: int = Query(default=0, ge=0), principal=Depends(identity)):
        return service.events(principal, request_id, after)

    @app.get("/api/v1/requests/{request_id}/events/stream")
    async def stream(request_id: str, request: Request, after: int = Query(default=0, ge=0),
                     last_event_id: str | None = Header(default=None), principal=Depends(identity)):
        service.get_request(principal, request_id)
        try:
            cursor = max(after, int(last_event_id or "0"))
        except ValueError as error:
            raise DomainError("INVALID_CURSOR", "Last-Event-ID must be an integer", 422) from error
        async def generate():
            nonlocal cursor
            # Short-lived authenticated SSE connection. Reconnect with Last-Event-ID.
            for _ in range(20):
                if await request.is_disconnected():
                    break
                for event in await asyncio.to_thread(service.events, principal, request_id, cursor):
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: audit\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    demo = ROOT / "apps" / "demo"
    if demo.exists():
        app.mount("/assets", StaticFiles(directory=demo), name="assets")
        @app.get("/", include_in_schema=False)
        def workbench():
            return FileResponse(demo / "index.html")
    return app


app = create_app()
