"""Advisory extraction contracts. Values are NOT confirmed procurement facts."""
from typing import Literal
from pydantic import Field, model_validator, field_validator
from .contracts import Contract, QuoteValues

FieldName = Literal['supplier_id', 'sku', 'quantity', 'uom', 'unit_price', 'tax_mode',
                    'tax_rate', 'shipping_cost', 'discount', 'delivery_days', 'currency']
FIELDS = frozenset(QuoteValues.model_fields) - {'lines'}


class Clarification(Contract):
    field: FieldName
    question: str = Field(min_length=5, max_length=500, strict=True)
    reason: Literal['missing', 'ambiguous', 'conflict']


class CandidateProposal(Contract):
    summary: str = Field(min_length=1, max_length=1800, strict=True)
    values: QuoteValues
    references: dict[FieldName, list[str]] = Field(max_length=12)
    context_evidence_ids: list[str] = Field(default_factory=list, max_length=16)
    clarifications: list[Clarification] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def grounded_shape(self):
        if self.values.lines is not None:
            raise ValueError('Candidate extraction currently supports single-item documents only')
        known = {k for k in FIELDS if getattr(self.values, k) is not None
                 and not (k == 'tax_mode' and self.values.tax_mode == 'unknown')}
        if set(self.references) != known:
            raise ValueError('Every proposed known field needs its own source references; unknown fields cannot be cited as facts')
        for refs in self.references.values():
            if not 1 <= len(refs) <= 4 or len(refs) != len(set(refs)) or any(
                    not isinstance(x, str) or not 1 <= len(x) <= 100 for x in refs):
                raise ValueError('Bounded unique fragment IDs required')
        if len(self.context_evidence_ids) != len(set(self.context_evidence_ids)) or any(
                not isinstance(x, str) or not 1 <= len(x) <= 100 for x in self.context_evidence_ids):
            raise ValueError('Context citations require bounded unique IDs')
        fields = [x.field for x in self.clarifications]
        if len(fields) != len(set(fields)) or set(fields) & known:
            raise ValueError('Ambiguous/conflicting fields must remain unknown, never a guessed value')
        return self

    @property
    def evidence_ids(self):
        return sorted({ref for refs in self.references.values() for ref in refs} | set(self.context_evidence_ids))


class CandidateReserve(Contract):
    document_id: str = Field(min_length=1, max_length=40, strict=True)
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=r'^[A-Za-z0-9_-]+$', strict=True)


class CandidateProcess(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    acknowledge_external_model: Literal[True]

    @field_validator('acknowledge_external_model', mode='before')
    @classmethod
    def explicit_bool(cls, value):
        if value is not True:
            raise ValueError('Explicit true acknowledgement required')
        return value


class CandidateImport(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    acknowledge_unverified_candidates: Literal[True]
    reason: str = Field(min_length=5, max_length=500, strict=True)

    @field_validator('acknowledge_unverified_candidates', mode='before')
    @classmethod
    def explicit_bool(cls, value):
        if value is not True:
            raise ValueError('Explicit true acknowledgement required')
        return value


class CandidateCancel(Contract):
    expected_revision: int = Field(ge=1, strict=True)


class CandidateEvidenceArgs(Contract):
    start: int = Field(default=0, ge=0, le=128, strict=True)
    limit: int = Field(default=16, ge=1, le=24, strict=True)
