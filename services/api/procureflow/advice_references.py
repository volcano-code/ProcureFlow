"""Version-bound reference reads shared by advice, HTTP and MCP.

A library is supporting evidence, never an executable rule or approval grant.
No model, network or write operation is performed by this module.
"""
from __future__ import annotations
from copy import deepcopy
import re
from pydantic import Field
from .contracts import Contract
from .domain import digest
from .errors import DomainError
from .policy_documents import PolicyDocumentService, SearchDocuments, sha

SCOPE = 'procurement'
FORMAT = 'advice-references-v1'
CITATION = re.compile(r'^pdr_[a-f0-9]{32}:[0-9]{1,3}$')
HASH = re.compile(r'^[a-f0-9]{64}$')


class ReferenceSearchArgs(Contract):
    query: str = Field(min_length=2, max_length=300, strict=True)
    limit: int = Field(default=5, ge=1, le=5, strict=True)


class ReferenceFragmentArgs(Contract):
    citation_id: str = Field(pattern=CITATION.pattern, strict=True)


def capture_binding(library: PolicyDocumentService, session, principal) -> dict:
    rows, signature, _ = library._snapshot(session, principal, SCOPE)
    return {'format': FORMAT, 'scope': SCOPE, 'snapshot_hash': signature,
        'method': 'hybrid' if library.encoder is not None else 'lexical',
        'encoder': library.encoder.identity if library.encoder is not None else None,
        'revisions': [{'revision_id': r.id, 'document_id': r.document_id,
            'version': r.version, 'source_hash': r.source_hash,
            'revision_hash': r.revision_hash} for r in rows], 'advisory_only': True}


def validate_binding(binding):
    """Validate even empty corpora. Missing binding never means 'use latest'."""
    if (type(binding) is not dict or binding.get('format') != FORMAT or
            binding.get('scope') != SCOPE or binding.get('advisory_only') is not True or
            binding.get('method') not in ('lexical', 'hybrid') or
            type(binding.get('revisions')) is not list or len(binding['revisions']) > 32 or
            (binding.get('method') == 'lexical' and binding.get('encoder') is not None) or
            (binding.get('method') == 'hybrid' and not isinstance(binding.get('encoder'), str))):
        raise DomainError('ADVICE_REFERENCE_INVALID', 'Invalid reference version binding', 409)
    ids = set()
    for row in binding['revisions']:
        if (type(row) is not dict or type(row.get('revision_id')) is not str or
                not CITATION.fullmatch(row['revision_id'] + ':0') or
                type(row.get('document_id')) is not str or not row['document_id'] or
                type(row.get('version')) is not int or row['version'] < 1 or
                row['revision_id'] in ids or any(type(row.get(k)) is not str or
                    not HASH.fullmatch(row[k]) for k in ('source_hash', 'revision_hash'))):
            raise DomainError('ADVICE_REFERENCE_INVALID', 'Invalid reference version binding', 409)
        ids.add(row['revision_id'])
    if digest([(r['revision_id'], r['revision_hash']) for r in binding['revisions']]) != binding.get('snapshot_hash'):
        raise DomainError('ADVICE_REFERENCE_INVALID', 'Invalid reference corpus fingerprint', 409)
    return binding


def validate_hit(binding, hit):
    """Verify identity and exact snippet bytes, not semantic entailment."""
    validate_binding(binding)
    rows = {r['revision_id']: r for r in binding['revisions']}
    if type(hit) is not dict or hit.get('revision_id') not in rows:
        raise DomainError('ADVICE_REFERENCE_INVALID', 'Reference is outside the bound corpus', 409)
    row, fragment = rows[hit['revision_id']], hit.get('fragment')
    if (any(hit.get(k) != row[k] for k in row) or hit.get('advisory_only') is not True or
            hit.get('trust') != 'reference_text_not_instructions' or type(fragment) is not dict or
            any(type(fragment.get(k)) is not int for k in ('ordinal', 'start', 'end', 'line_start', 'line_end')) or
            not 0 <= fragment['ordinal'] < 100 or not 0 <= fragment['start'] < fragment['end'] <= 12000 or
            not 1 <= fragment['line_start'] <= fragment['line_end'] or
            type(fragment.get('text')) is not str or not 1 <= len(fragment['text']) <= 400 or
            len(fragment['text']) != fragment['end'] - fragment['start'] or
            sha(fragment['text']) != fragment.get('sha256') or
            hit.get('citation_id') != hit['revision_id'] + ':' + str(fragment['ordinal'])):
        raise DomainError('ADVICE_REFERENCE_INVALID', 'Reference fragment integrity failed', 409)
    return hit


def validate_result(binding, result, *, limit=5):
    validate_binding(binding)
    if (type(result) is not dict or result.get('snapshot_hash') != binding['snapshot_hash'] or
            result.get('method') != binding['method'] or result.get('encoder') != binding['encoder'] or
            result.get('advisory_only') is not True or type(result.get('results')) is not list or
            len(result['results']) > limit or
            result.get('status') != ('matches' if result['results'] else 'no_match')):
        raise DomainError('ADVICE_REFERENCE_INVALID', 'Reference response binding failed', 409)
    ids = []
    for hit in result['results']:
        validate_hit(binding, hit)
        ids.append(hit['citation_id'])
    if len(set(ids)) != len(ids):
        raise DomainError('ADVICE_REFERENCE_INVALID', 'Duplicate reference fragments', 409)
    return result


def fragment_result(library, principal, binding, citation_id):
    validate_binding(binding)
    if not CITATION.fullmatch(citation_id):
        raise DomainError('TOOL_ARGUMENTS_INVALID', 'Invalid reference fragment identifier', 422)
    revision_id, ordinal_text = citation_id.split(':')
    if revision_id not in {r['revision_id'] for r in binding['revisions']}:
        raise DomainError('TOOL_SCOPE_DENIED', 'Reference is outside this advice run', 403)
    row = library.revision(principal, revision_id)
    ordinal = int(ordinal_text)
    if row['status'] != 'active':
        raise DomainError('ADVICE_INPUT_CHANGED', 'Reference is no longer current', 409)
    if ordinal >= len(row['fragments']):
        raise DomainError('EVIDENCE_NOT_FOUND', 'Reference fragment not found', 404)
    hit = {k: row[k] for k in ('document_id', 'version', 'source_hash', 'revision_hash',
        'title', 'source_label', 'effective_at', 'expires_at')}
    hit.update(revision_id=revision_id, citation_id=citation_id, fragment=deepcopy(row['fragments'][ordinal]),
        trust='reference_text_not_instructions', advisory_only=True)
    validate_hit(binding, hit)
    return {'method': binding['method'], 'encoder': binding['encoder'],
        'snapshot_hash': binding['snapshot_hash'], 'status': 'matches',
        'results': [hit], 'advisory_only': True}
