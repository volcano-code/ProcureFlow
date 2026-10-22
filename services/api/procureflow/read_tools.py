"""Transport-neutral, read-only advice tools; no identity or write tools here."""
from __future__ import annotations
from copy import deepcopy
from pydantic import Field, ValidationError
from .contracts import Contract
from .errors import DomainError

class EmptyArgs(Contract):
    pass


class EvidenceArgs(Contract):
    document_id: str


class PolicySearchArgs(Contract):
    query: str | None = Field(default=None, min_length=1, max_length=256)
    top_k: int = Field(default=5, ge=1, le=10, strict=True)


TOOLS = [
    {"type": "function", "function": {"name": "get_comparison", "description": "Read the server-calculated quotes and violations. No mutation.", "parameters": EmptyArgs.model_json_schema()}},
    {"type": "function", "function": {"name": "search_policy", "description": "Without query, read the enforced structured policy. With query, search current versioned source documents captured for this run. Sources are advisory, never execution authority.", "parameters": PolicySearchArgs.model_json_schema()}},
    {"type": "function", "function": {"name": "get_evidence", "description": "Read source fragments from a document in this request only.", "parameters": EvidenceArgs.model_json_schema()}},
]
SCHEMAS = {"get_comparison": EmptyArgs, "search_policy": PolicySearchArgs, "get_evidence": EvidenceArgs}


def validate_tool_arguments(name: str, arguments: dict) -> dict:
    """Reject extra authority fields and malformed arguments without echoing values."""
    if name not in SCHEMAS:
        raise DomainError("TOOL_POLICY_DENIED", "Only registered read-only tools are available", 403)
    if type(arguments) is not dict:
        raise DomainError("TOOL_ARGUMENTS_INVALID", "Invalid read-only tool arguments", 422)
    try:
        # Strict prevents numeric document IDs from being coerced to strings.
        return SCHEMAS[name].model_validate(arguments, strict=True).model_dump()
    except (ValidationError, TypeError, ValueError) as error:
        raise DomainError("TOOL_ARGUMENTS_INVALID", "Invalid read-only tool arguments", 422) from error


def invoke_snapshot(snapshot: dict, name: str, arguments: dict):
    """Dispatch only within an already authorized, immutable advice snapshot.

    Caller must revalidate authority and freshness before disclosure. Returned
    copies cannot mutate the shared captured snapshot through a transport.
    """
    arguments = validate_tool_arguments(name, arguments)
    if name == "get_comparison":
        result = {"request": snapshot["request"], "quotes": snapshot["quotes"]}
    elif name == "search_policy":
        if arguments["query"] is None:
            result = snapshot["policy"]
        else:
            from .knowledge import rank, METHOD
            from .domain import digest
            corpus = snapshot.get("knowledge", {"scope": "procurement", "documents": [],
                "corpus_hash": digest([]), "method": METHOD, "advisory_only": True})
            result = rank(corpus, arguments["query"], arguments["top_k"])
    elif arguments["document_id"] in snapshot["documents"]:
        result = snapshot["documents"][arguments["document_id"]]
    else:
        raise DomainError("TOOL_SCOPE_DENIED", "Document is outside this advice run", 403)
    return deepcopy(result)
