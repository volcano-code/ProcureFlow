"""Persisted mapping previews, reusing the request aggregate and quote history."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import func, select
from .db import DocumentRow, QuoteRow, QuoteVersionRow, TableImportRow, audit, uid
from .errors import DomainError
from .tabular import map_table, parse_table

PREVIEW_TTL_MINUTES = 30
MAX_PENDING_IMPORTS_PER_TENANT = 100


class TableImportServiceMixin:
    def _table_import(self, session, principal, import_id):
        row = session.scalar(select(TableImportRow).where(TableImportRow.id == import_id,
            TableImportRow.tenant_id == principal.tenant_id).execution_options(populate_existing=True))
        if row is None:
            raise DomainError("NOT_FOUND", "Table import not found in this workspace", 404)
        return row

    def _table_import_open(self, row):
        if row.status != "OPEN":
            raise DomainError("IMPORT_ALREADY_CONFIRMED", "This import already created a quote; edit the quote with a correction reason")
        if datetime.fromisoformat(row.expires_at) <= datetime.now(timezone.utc):
            raise DomainError("TABLE_IMPORT_EXPIRED", "The preview expired; upload the source again and review a new mapping", 410)

    def _table_import_view(self, row, request):
        from .service import FROZEN
        parsed = row.parsed or {}
        sheets = row.table["sheets"]
        return {"id": row.id, "request_id": row.request_id, "revision": row.revision,
            "status": row.status, "expires_at": row.expires_at, "filename": row.filename,
            "document_sha256": row.sha256, "sheets": sheets,
            "suggested_mapping": sheets[0].get("suggested_mapping", {}) if sheets else {},
            "selection": row.selection, "values": parsed.get("values"), "evidence": parsed.get("evidence"),
            "issues": sorted(set(row.table.get("issues", []) + parsed.get("issues", []))),
            "can_confirm": bool(row.parsed and row.status == "OPEN" and request.status not in FROZEN
                and row.request_version == request.version
                and datetime.fromisoformat(row.expires_at) > datetime.now(timezone.utc)),
            "quote_id": row.quote_id}

    def upload_table_import(self, principal, request_id, filename, data):
        from .service import require
        require(principal, "buyer")
        if not data or len(data) > self.settings.max_upload_bytes:
            raise DomainError("UPLOAD_LIMIT", "The file must be non-empty and no larger than 2 MiB", 413)
        filename = Path(filename.replace("\\", "/")).name[:160]
        sha = hashlib.sha256(data).hexdigest()
        # Authorize before spawning a parser; do not hold a database write lock while parsing.
        with self.transaction(principal) as session:
            self._mutable(self._request(session, principal, request_id))
        table = parse_table(filename, data)
        import_id = uid("tim_")
        path = self.document_dir / (import_id + Path(filename).suffix.lower())
        created_file = False
        try:
            with self.transaction(principal, write=True) as session:
                request = self._request(session, principal, request_id, lock=True)
                self._mutable(request)
                row = session.scalar(select(TableImportRow).where(TableImportRow.tenant_id == principal.tenant_id,
                    TableImportRow.request_id == request_id, TableImportRow.sha256 == sha))
                if row:
                    self._verify_document(row)
                    if row.status == "OPEN" and datetime.fromisoformat(row.expires_at) <= datetime.now(timezone.utc):
                        row.revision += 1
                        row.selection, row.parsed = None, None
                        row.request_version = request.version
                        row.expires_at = (datetime.now(timezone.utc) + timedelta(minutes=PREVIEW_TTL_MINUTES)).isoformat()
                        audit(session, principal, request.id, "TABLE_IMPORT_REOPENED", {"import_id": row.id, "revision": row.revision})
                    return self._table_import_view(row, request)
                if session.scalar(select(DocumentRow.id).where(DocumentRow.tenant_id == principal.tenant_id,
                        DocumentRow.request_id == request_id, DocumentRow.sha256 == sha)):
                    raise DomainError("SOURCE_ALREADY_IMPORTED", "This source already has a quote; edit that quote with a correction reason")
                count = session.scalar(select(func.count()).select_from(TableImportRow).where(
                    TableImportRow.tenant_id == principal.tenant_id, TableImportRow.status == "OPEN"))
                if count >= MAX_PENDING_IMPORTS_PER_TENANT:
                    raise DomainError("TABLE_IMPORT_LIMIT", "Workspace pending-import retention limit reached; operator cleanup is required", 413)
                # O_EXCL avoids overwriting another source. Files are never served statically.
                with path.open("xb") as target:
                    created_file = True
                    path.chmod(0o600)
                    target.write(data)
                row = TableImportRow(id=import_id, tenant_id=principal.tenant_id, request_id=request_id,
                    filename=filename, sha256=sha, storage_key=path.name, table=table, selection=None, parsed=None,
                    revision=1, request_version=request.version, status="OPEN",
                    expires_at=(datetime.now(timezone.utc) + timedelta(minutes=PREVIEW_TTL_MINUTES)).isoformat())
                session.add(row)
                session.flush()
                audit(session, principal, request.id, "TABLE_IMPORT_UPLOADED", {"import_id": row.id, "sha256": sha})
                return self._table_import_view(row, request)
        except Exception:
            if created_file:
                path.unlink(missing_ok=True)
            raise

    def get_table_import(self, principal, import_id):
        with self.transaction(principal) as session:
            row = self._table_import(session, principal, import_id)
            self._verify_document(row)
            request = self._request(session, principal, row.request_id)
            return self._table_import_view(row, request)

    def preview_table_import(self, principal, import_id, command):
        from .service import require
        require(principal, "buyer")
        with self.transaction(principal, write=True) as session:
            row = self._table_import(session, principal, import_id)
            request = self._request(session, principal, row.request_id, lock=True)
            row = self._table_import(session, principal, import_id)
            self._mutable(request)
            self._table_import_open(row)
            if command.expected_revision != row.revision:
                raise DomainError("VERSION_CONFLICT", "Refresh the import before changing its mapping")
            self._verify_document(row)
            selection = command.model_dump(exclude={"expected_revision"})
            parsed = map_table(row.table, document_id="doc_" + row.id[4:], sha=row.sha256, **selection)
            # An explicit row cannot smuggle a different purchase through a chosen mapping.
            values = parsed["values"]
            if values.get("sku") is not None and values["sku"] != request.data["sku"]:
                raise DomainError("IMPORT_SKU_MISMATCH", "The selected quote SKU must match the single-SKU request", 422)
            for field, expected in (("currency", "CNY"), ("uom", "EA")):
                if values.get(field) is not None and values[field] != expected:
                    raise DomainError("IMPORT_UNSUPPORTED_UNIT", "Only CNY and EA quotes are supported", 422)
            row.selection, row.parsed = selection, parsed
            row.revision += 1
            row.request_version = request.version
            audit(session, principal, request.id, "TABLE_IMPORT_MAPPED", {"import_id": row.id, "revision": row.revision,
                "selection": selection, "sha256": row.sha256, "issues": parsed["issues"]})
            return self._table_import_view(row, request)

    def confirm_table_import(self, principal, import_id, command):
        from .service import require
        require(principal, "buyer")
        with self.transaction(principal, write=True) as session:
            row = self._table_import(session, principal, import_id)
            request = self._request(session, principal, row.request_id, lock=True)
            row = self._table_import(session, principal, import_id)
            if command.expected_revision != row.revision:
                raise DomainError("VERSION_CONFLICT", "Only the displayed mapping revision may be imported")
            self._verify_document(row)
            if row.status == "IMPORTED":
                # Same revision is an idempotent readback, including a lost response or later request execution.
                quote, version = self._quote(session, principal, row.quote_id)
                return self._quote_view(session, quote, version, request)
            self._mutable(request)
            self._table_import_open(row)
            if not row.parsed:
                raise DomainError("IMPORT_NOT_PREVIEWED", "Choose a worksheet, row and column mapping and preview it first")
            if row.request_version != request.version:
                raise DomainError("IMPORT_REQUEST_STALE", "Request inputs changed; preview the mapping again before importing")
            if session.scalar(select(DocumentRow.id).where(DocumentRow.tenant_id == principal.tenant_id,
                    DocumentRow.request_id == request.id, DocumentRow.sha256 == row.sha256)):
                raise DomainError("SOURCE_ALREADY_IMPORTED", "This source already has a quote; edit that quote with a correction reason")
            parsed = row.parsed
            document = DocumentRow(id="doc_" + row.id[4:], tenant_id=principal.tenant_id, request_id=request.id,
                filename=row.filename, sha256=row.sha256, storage_key=row.storage_key, fragments=parsed["fragments"])
            quote = QuoteRow(id=uid("quo_"), tenant_id=principal.tenant_id, request_id=request.id, current_version=1)
            session.add_all([document, quote])
            session.flush()
            version = QuoteVersionRow(id=uid("qv_"), quote_id=quote.id, document_id=document.id, version=1,
                values=parsed["values"], evidence=parsed["evidence"], issues=parsed["issues"], confirmed_by=None)
            session.add(version)
            row.status, row.quote_id = "IMPORTED", quote.id
            session.flush()
            self._invalidate(session, request)
            audit(session, principal, request.id, "QUOTE_IMPORTED", {"quote_id": quote.id, "version_id": version.id,
                "document_id": document.id, "sha256": row.sha256, "parser": parsed["parser"],
                "import_id": row.id, "import_revision": row.revision, "selection": row.selection, "issues": parsed["issues"]})
            return self._quote_view(session, quote, version, request)
