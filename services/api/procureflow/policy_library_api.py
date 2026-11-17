"""Separate authenticated OpenAPI surface for the reference library."""
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from .errors import DomainError
from .policy_documents import PolicyDocumentService, PublishDocument, SearchDocuments


def create_library_api(procurement, identity, encoder=None, advice_service=None):
    service = PolicyDocumentService(procurement, encoder)
    app = FastAPI(title='ProcureFlow Reference Library', version='0.1.0',
        openapi_url=None, docs_url=None, redoc_url=None)
    app.state.library = service

    @app.exception_handler(DomainError)
    async def domain_error(_, error):
        return JSONResponse(status_code=error.status_code, content={'error': {
            'code': error.code, 'message': error.message}})

    @app.get('/openapi.json')
    def schema(principal=Depends(identity)):
        return app.openapi()

    @app.get('/documents')
    def history(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1600), principal=Depends(identity)):
        return service.history(principal, limit, offset)

    @app.post('/documents/{document_key}/versions', status_code=201)
    def publish(document_key: str, command: PublishDocument, principal=Depends(identity)):
        return service.publish(principal, document_key, command)

    @app.get('/revisions/{revision_id}')
    def revision(revision_id: str, principal=Depends(identity)):
        return service.revision(principal, revision_id)

    @app.post('/search')
    def search(command: SearchDocuments, principal=Depends(identity)):
        return service.search(principal, command)
    def bound_advice():
        if advice_service is not None:
            return advice_service
        from .advice import AdviceService
        return AdviceService(procurement, service)

    @app.get('/advice-runs/{run_id}/search')
    def bound_search(run_id: str, query: str = Query(min_length=2, max_length=300),
                     limit: int = Query(5, ge=1, le=5), principal=Depends(identity)):
        return bound_advice().reference_tool(principal, run_id, 'search_references', {'query': query, 'limit': limit})

    @app.get('/advice-runs/{run_id}/fragment')
    def bound_fragment(run_id: str, citation_id: str = Query(min_length=1, max_length=80), principal=Depends(identity)):
        return bound_advice().reference_tool(principal, run_id, 'get_reference_fragment', {'citation_id': citation_id})
    return app
