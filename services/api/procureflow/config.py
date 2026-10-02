from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from .database_config import normalize_database_url

DEMO_IDENTITIES = {
    "demo-buyer": {"user_id": "buyer-01", "tenant_id": "demo", "role": "buyer"},
    "demo-approver": {"user_id": "approver-01", "tenant_id": "demo", "role": "approver"},
    "demo-auditor": {"user_id": "auditor-01", "tenant_id": "demo", "role": "auditor"},
    "demo-other-tenant": {"user_id": "other-01", "tenant_id": "other", "role": "buyer"},
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PF_DATA_DIR", ".data")).resolve())
    database_url: str = field(default_factory=lambda: os.getenv("PF_DATABASE_URL", ""), repr=False)
    mode: str = field(default_factory=lambda: os.getenv("PF_MODE", "demo"))
    auth_tokens: dict = field(default_factory=dict, repr=False)
    web_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        value.strip() for value in os.getenv("PF_WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if value.strip()))
    approval_ttl_seconds: int = 3600
    max_upload_bytes: int = 2 * 1024 * 1024
    erp_mode: str = field(default_factory=lambda: os.getenv("PF_ERP_MODE", "mock"))
    erp_url: str = field(default_factory=lambda: os.getenv("ERP_BASE_URL", ""))
    erp_api_key: str = field(default_factory=lambda: os.getenv("ERP_API_KEY", ""), repr=False)
    erp_api_secret: str = field(default_factory=lambda: os.getenv("ERP_API_SECRET", ""), repr=False)
    erp_company: str = field(default_factory=lambda: os.getenv("ERP_COMPANY", ""))
    erp_tax_account: str = field(default_factory=lambda: os.getenv("ERP_TAX_ACCOUNT", ""))
    erp_freight_account: str = field(default_factory=lambda: os.getenv("ERP_FREIGHT_ACCOUNT", ""))
    erp_allow_draft_writes: bool = field(default_factory=lambda: os.getenv("ERP_ALLOW_DRAFT_WRITES", "false").lower() == "true")

    def __post_init__(self):
        if self.mode not in {"demo", "private"}:
            raise ValueError("PF_MODE must be demo or private; public production deployment is not supported yet")
        tokens = self.auth_tokens or json.loads(os.getenv("PF_AUTH_TOKENS", "{}"))
        if not tokens and self.mode == "demo":
            tokens = {k: dict(v) for k, v in DEMO_IDENTITIES.items()}
        if not tokens:
            raise ValueError("Private mode requires PF_AUTH_TOKENS; no implicit identity is permitted")
        for token, identity in tokens.items():
            if not token or not all(identity.get(k) for k in ("user_id", "tenant_id", "role")):
                raise ValueError("Malformed token-to-principal configuration")
            if identity["role"] not in {"buyer", "approver", "auditor"}:
                raise ValueError("Unknown configured role")
            if self.mode == "private" and (token.startswith("demo-") or len(token) < 32):
                raise ValueError("Private-mode tokens must be at least 32 characters and not demo tokens")
        if self.erp_mode not in {"mock", "erpnext"}:
            raise ValueError("Unknown ERP mode")
        if self.erp_mode == "erpnext" and self.mode != "private":
            raise ValueError("ERPNext requires PF_MODE=private and non-demo identities, including read-only access")
        if self.erp_mode == "mock" and self.erp_allow_draft_writes:
            raise ValueError("ERP_ALLOW_DRAFT_WRITES is only valid with PF_ERP_MODE=erpnext")
        for origin in self.web_origins:
            parsed = urlparse(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
                    or parsed.path != "" or parsed.query or parsed.fragment or "*" in origin):
                raise ValueError("PF_WEB_ORIGINS must be explicit HTTP(S) origins without credentials or paths")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "auth_tokens", tokens)
        url = self.database_url or f"sqlite:///{self.data_dir / 'procureflow.sqlite3'}"
        object.__setattr__(self, "database_url", normalize_database_url(url))
