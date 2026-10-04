from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def decimal_string(value):
    if value is None:
        return value
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Decimal values must be JSON strings, never floating-point JSON numbers")
    try:
        value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid decimal string") from error
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    return value


Money = Annotated[Decimal, BeforeValidator(decimal_string), Field(ge=0, le=Decimal('1000000000'), max_digits=15, decimal_places=2)]
Quantity = Annotated[Decimal, BeforeValidator(decimal_string), Field(gt=0, le=Decimal('1000000'), max_digits=10, decimal_places=3)]
Rate = Annotated[Decimal, BeforeValidator(decimal_string), Field(ge=0, le=1, max_digits=7, decimal_places=6)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Principal(Contract):
    user_id: str
    tenant_id: str
    role: Literal["buyer", "approver", "auditor"]


class RequestCreate(Contract):
    title: str = Field(min_length=1, max_length=120)
    sku: str = Field(min_length=1, max_length=80)
    quantity: Quantity
    uom: Literal["EA"] = "EA"
    budget: Money
    max_delivery_days: int = Field(default=14, ge=1, le=365, strict=True)
    currency: Literal["CNY"] = "CNY"


class RequestUpdate(RequestCreate):
    expected_version: int = Field(ge=1, strict=True)


class PolicyVersionCreate(Contract):
    expected_version: int = Field(ge=1, strict=True)
    budget_cap: Money | None = None
    max_delivery_days: int | None = Field(default=None, ge=1, le=365, strict=True)
    minimum_valid_quotes: int = Field(ge=1, le=100, strict=True)
    effective_at: datetime | None = None
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("effective_at", mode="before")
    @classmethod
    def timestamp_string(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Effective time must be an ISO timestamp with a timezone")
        return value

    @field_validator("effective_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Effective time must include a timezone")
        return value


class QuoteValues(Contract):
    supplier_id: str | None = Field(default=None, min_length=1, max_length=80)
    sku: str | None = Field(default=None, min_length=1, max_length=80)
    quantity: Quantity | None = None
    uom: str | None = Field(default=None, min_length=1, max_length=12)
    unit_price: Money | None = None
    tax_mode: Literal["included", "excluded", "unknown"] = "unknown"
    tax_rate: Rate | None = None
    shipping_cost: Money | None = None
    discount: Money | None = None
    delivery_days: int | None = Field(default=None, ge=1, le=365, strict=True)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    # ERP document names are opaque identifiers, not case-insensitive codes.
    # Preserve supplier_id and sku through extraction, approval and readback.
    @field_validator("uom", "currency")
    @classmethod
    def clean_units_and_currency(cls, value):
        return value.upper() if value is not None else value


class QuoteEdit(Contract):
    expected_version: int = Field(ge=1, strict=True)
    values: QuoteValues
    reason: str = Field(min_length=5, max_length=500)


class QuoteConfirm(Contract):
    expected_version: int = Field(ge=1, strict=True)
    acknowledge: Literal[True]


class AnalyzeCommand(Contract):
    preferred_quote_id: str | None = None


class AdviceRunCommand(Contract):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$", strict=True)


class ApprovalCommand(Contract):
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"] = "approve"
    note: str = Field(default="", max_length=500)


class ExecuteCommand(Contract):
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Narrative(Contract):
    summary: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class TableImportPreview(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    sheet: str = Field(min_length=1, max_length=120)
    header_row: int = Field(ge=1, le=10000, strict=True)
    row: int = Field(ge=1, le=10000, strict=True)
    mapping: dict[str, str] = Field(min_length=1, max_length=12)

    @field_validator("mapping")
    @classmethod
    def bounded_mapping(cls, value):
        import re
        if any(key not in QuoteValues.model_fields or not re.fullmatch(r"[A-Z]{1,3}", col) for key, col in value.items()):
            raise ValueError("Only supported quote fields and canonical spreadsheet columns are accepted")
        if len(set(value.values())) != len(value):
            raise ValueError("Each source column may map to only one field")
        return value


class TableImportConfirm(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    acknowledge: Literal[True]
