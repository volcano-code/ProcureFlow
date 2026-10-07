from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy.exc import SQLAlchemyError
from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartException
from . import __version__
from .agent import LANGGRAPH_VERSION, RUNTIME, ReadOnlyAgent
from .advice import AdviceService
from .auth import IdentityService
from .config import Settings
from .contracts import (AdviceRunCommand, AnalyzeCommand, ApprovalCommand, ExecuteCommand, LoginCommand, Principal,
    PolicyVersionCreate, QuoteConfirm, QuoteEdit, RequestCreate, RequestUpdate, TableImportPreview, TableImportConfirm)
from .db import Database, uid
from .erp import ERPNextClient, ERPRejected, ERPUnknown, MockERP
from .errors import DomainError
from .service import ProcurementService, require

ROOT = Path(__file__).resolve().parents[3]


class BoundedRequestBody:
    """Enforce a streaming byte budget before multipart parsing/spooling.

    This bounds bytes delivered to the application; upstream server/proxy buffering,
    connection counts and slow uploads still require deployment-level controls.
    """
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        received, exceeded = 0, False

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    exceeded = True
                    # Starlette closes partial spooled files on this exception.
                    raise MultiPartException("Request body too large")
            return message

        async def bounded_send(message):
            if exceeded:
                if message["type"] == "http.response.start":
                    # Form/JSON decoders translate parse failures to 400; retain a
                    # stable 413 contract without leaking parser implementation.
                    await JSONResponse(status_code=413, content={"error": {
                        "code": "BODY_LIMIT", "message": "Request body too large"}})(scope, receive, send)
                return
            await send(message)

        return await self.app(scope, bounded_receive, bounded_send)


def create_app(settings: Settings | None = None, database: Database | None = None, erp=None) -> FastAPI:
    settings = settings or Settings()
    db = database or Database(settings.database_url,
        create_schema=settings.mode == "demo" and settings.database_url.startswith("sqlite"))
    if erp is None:
        erp = MockERP(settings.data_dir / "mock-erp.sqlite3") if settings.erp_mode == "mock" else ERPNextClient(
            settings.erp_url, settings.erp_api_key, settings.erp_api_secret, settings.erp_company, settings.erp_allow_draft_writes,
            tax_account=settings.erp_tax_account, freight_account=settings.erp_freight_account)
    service = ProcurementService(db, settings, erp)
    advice_service = AdviceService(service)
    identities = IdentityService(db, settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.engine.dispose()
        if hasattr(erp, "client"):
            erp.client.close()

    app = FastAPI(title="ProcureFlow", version=__version__, lifespan=lifespan,
        description="Evidence-first procurement local alpha. Mock ERP and deterministic baseline by default; no live ordering.")
    app.state.service, app.state.settings = service, settings
    app.state.identities = identities
    app.add_middleware(BoundedRequestBody, limit=settings.max_upload_bytes + 65536)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.web_origins),
                       allow_credentials=False, allow_methods=["GET", "POST", "PUT"],
                       allow_headers=["Authorization", "Content-Type", "Last-Event-ID"])
    bearer = HTTPBearer(auto_error=False)

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise DomainError("UNAUTHENTICATED", "A bearer token is required", 401)
        # No client-supplied tenant, role or approver ID is trusted.
        return identities.authenticate(credentials.credentials)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == "/api/v1/auth/login":
            # FastAPI's default validation response echoes input values, which
            # must never reflect an invitation or any extra credential fields.
            return JSONResponse(status_code=422, content={"error": {
                "code": "INVALID_LOGIN", "message": "Provide one valid invitation credential"}})
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(DomainError)
    async def domain_error(_, error):
        return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": error.message}})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if settings.mode == "pilot" and request.url.path in {"/api/v1/auth/login", "/api/v1/auth/logout"}:
            origin = request.headers.get("origin")
            if origin and origin not in {*settings.web_origins, str(request.base_url).rstrip("/")}:
                return JSONResponse(status_code=403, content={"error": {
                    "code": "ORIGIN_DENIED", "message": "Browser origin is not allowed"}})
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

    @app.get("/api/v1/auth/config")
    def auth_config():
        return {"mode": settings.mode, "login_method": "invite" if settings.mode == "pilot" else "static_bearer",
                "session_ttl_seconds": settings.session_ttl_seconds if settings.mode == "pilot" else None}

    @app.post("/api/v1/auth/login")
    def login(command: LoginCommand):
        return identities.login(command.credential.get_secret_value())

    @app.post("/api/v1/auth/logout")
    def logout(principal=Depends(identity)):
        identities.logout(principal)
        return {"status": "logged_out"}

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
        return {**principal.model_dump(), **({"expires_at": principal.session_expires_at} if settings.mode == "pilot" else {})}

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
        # Streaming ingress cap precedes multipart parsing; decoding runs in a bounded subprocess.
        require(principal, "buyer")
        data = await file.read(settings.max_upload_bytes + 1)
        await file.close()
        return await asyncio.to_thread(service.import_quote, principal, request_id, file.filename or "upload.txt", data)

    @app.post("/api/v1/requests/{request_id}/table-imports", status_code=201)
    async def upload_table(request_id: str, file: UploadFile = File(...), principal=Depends(identity)):
        require(principal, "buyer")
        data = await file.read(settings.max_upload_bytes + 1)
        await file.close()
        return await asyncio.to_thread(service.upload_table_import, principal, request_id, file.filename or "upload.csv", data)

    @app.get("/api/v1/table-imports/{import_id}")
    def get_table_import(import_id: str, principal=Depends(identity)):
        return service.get_table_import(principal, import_id)

    @app.post("/api/v1/table-imports/{import_id}/preview")
    def preview_table_import(import_id: str, command: TableImportPreview, principal=Depends(identity)):
        return service.preview_table_import(principal, import_id, command)

    @app.post("/api/v1/table-imports/{import_id}/confirm")
    def confirm_table_import(import_id: str, command: TableImportConfirm, principal=Depends(identity)):
        return service.confirm_table_import(principal, import_id, command)

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
        return service.policy(principal)

    @app.get("/api/v1/policy/versions")
    def policy_history(principal=Depends(identity)):
        return service.policy_history(principal)

    @app.post("/api/v1/policy/versions", status_code=201)
    def publish_policy(command: PolicyVersionCreate, principal=Depends(identity)):
        return service.publish_policy(principal, command)

    @app.get("/api/v1/requests/{request_id}/evaluations")
    def evaluations(request_id: str, offset: int = Query(default=0, ge=0),
                    limit: int = Query(default=100, ge=1, le=200), principal=Depends(identity)):
        return service.evaluations(principal, request_id, offset=offset, limit=limit)

    @app.get("/api/v1/suppliers")
    def suppliers(principal=Depends(identity)):
        try:
            items = erp.suppliers()
            with db.transaction() as session:
                identities.validate_principal(session, principal)
            return {"mode": erp.mode, "items": items, "limit": 100, "may_have_more": erp.mode == "erpnext"}
        except (ERPRejected, ERPUnknown) as error:
            raise DomainError("ERP_READ_FAILED", "Supplier lookup failed; check server-side ERP configuration", 502) from error

    @app.post("/api/v1/requests/{request_id}/analyze")
    def analyze(request_id: str, command: AnalyzeCommand, principal=Depends(identity)):
        return service.analyze(principal, request_id, command.preferred_quote_id)

    @app.post("/api/v1/requests/{request_id}/advice")
    def advice(request_id: str, principal=Depends(identity)):
        require(principal, "buyer")
        # Legacy synchronous route uses the same durable, immutable capture as runs.
        request = service.get_request(principal, request_id)
        receipt = advice_service.reserve(principal, request_id, AdviceRunCommand(
            expected_version=request["version"], idempotency_key=uid("legacy_")))
        result = advice_service.process(principal, receipt["id"], ReadOnlyAgent.from_env)
        if result["status"] != "COMPLETED" or not result["current"]:
            code = result["error_code"] or "ADVICE_INPUT_CHANGED"
            raise DomainError(code, "Advice did not complete against current bound inputs",
                              503 if code == "MODEL_NOT_CONFIGURED" else 409)
        return {**result["output"], "run_id": result["id"], "input_hash": result["input_hash"],
                "policy_version": result["policy_version"], "policy_hash": result["policy_hash"],
                "quote_collection_hash": result["quote_collection_hash"], "current": True}

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

    @app.get("/api/v1/requests/{request_id}/approvals")
    def approval_history(request_id: str, offset: int = Query(default=0, ge=0),
                         limit: int = Query(default=100, ge=1, le=200), principal=Depends(identity)):
        return service.approvals(principal, request_id, offset=offset, limit=limit)

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
                try:
                    # service.events revalidates durable authority in its own
                    # transaction on every poll; a long stream cannot cache it.
                    batch = await asyncio.to_thread(service.events, principal, request_id, cursor)
                except DomainError as error:
                    if error.status_code != 401:
                        raise
                    yield "event: auth_invalid\ndata: " + json.dumps({"code": "UNAUTHENTICATED",
                        "message": "Session is no longer valid; sign in again"}) + "\n\n"
                    return
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: audit\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    demo = ROOT / "apps" / "demo"
    if demo.exists() and settings.mode != "pilot":
        app.mount("/assets", StaticFiles(directory=demo), name="assets")
        @app.get("/", include_in_schema=False)
        def workbench():
            return FileResponse(demo / "index.html")
    return app


app = create_app()
