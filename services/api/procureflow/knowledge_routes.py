"""Authenticated document routes; mounted by the existing application factory."""
from fastapi import Depends, Query
from .knowledge import NewDocumentVersion, PublishDocument, DocumentTransition


def register_knowledge_routes(app, knowledge, identity):
    @app.get("/api/v1/knowledge/documents")
    def knowledge_documents(principal=Depends(identity)):
        return knowledge.list(principal)

    @app.post("/api/v1/knowledge/versions", status_code=201)
    def knowledge_create(command: NewDocumentVersion, principal=Depends(identity)):
        return knowledge.create(principal, command)

    @app.post("/api/v1/knowledge/versions/{version_id}/publish")
    def knowledge_publish(version_id: str, command: PublishDocument, principal=Depends(identity)):
        return knowledge.transition(principal, version_id, command, publish=True)

    @app.post("/api/v1/knowledge/versions/{version_id}/retire")
    def knowledge_retire(version_id: str, command: DocumentTransition, principal=Depends(identity)):
        return knowledge.transition(principal, version_id, command)

    @app.get("/api/v1/knowledge/search")
    def knowledge_search(q: str = Query(min_length=1, max_length=256),
            scope: str = Query(default="procurement", pattern=r"^[a-z][a-z0-9_-]{0,79}$"),
            top_k: int = Query(default=5, ge=1, le=10), principal=Depends(identity)):
        return knowledge.search(principal, q, scope, top_k)

    @app.get("/api/v1/knowledge/citations/{citation_id}")
    def knowledge_citation(citation_id: str,
            corpus_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
            scope: str = Query(default="procurement", pattern=r"^[a-z][a-z0-9_-]{0,79}$"),
            principal=Depends(identity)):
        return knowledge.citation(principal, citation_id, corpus_hash, scope)

