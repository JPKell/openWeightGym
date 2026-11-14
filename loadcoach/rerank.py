"""Bounded Jina/Cohere-shaped rerank requests and the public short-path alias."""


def checked(body):
    """Validate the text-only public contract while preserving the forwarded fields exactly."""
    if not isinstance(body, dict) or body.keys() - {'model', 'query', 'documents', 'top_n', 'return_documents'}:
        raise ValueError('Rerank accepts model, query, documents, top_n and return_documents.')
    if not isinstance(body.get('query'), str) or not body['query'].strip():
        raise ValueError('query must be nonempty text.')
    documents = body.get('documents')
    if not isinstance(documents, list) or not 1 <= len(documents) <= 1000 or any(not isinstance(one, str) for one in documents):
        raise ValueError('documents must contain 1–1000 strings.')
    if len(body['query'].encode()) + sum(len(one.encode()) for one in documents) > 32 * 1024 * 1024:
        raise ValueError('Rerank text must fit within 32 MiB.')
    if 'top_n' in body and (type(body['top_n']) is not int or not 1 <= body['top_n'] <= 1000):
        raise ValueError('top_n must be a whole number from 1 to 1000.')
    if 'return_documents' in body and type(body['return_documents']) is not bool:
        raise ValueError('return_documents must be true or false.')
    return body


class RerankAlias:
    """Normalize /rerank before authentication, route tracing and ordinary inference dispatch."""
    def __init__(self, app):
        """Wrap an existing gateway without adding another inference path."""
        self.app = app

    async def __call__(self, scope, receive, send):
        """Keep query strings and all caller context while selecting the canonical endpoint."""
        if scope['type'] == 'http' and scope.get('path') == '/rerank':
            scope.update(path='/v1/rerank', raw_path=b'/v1/rerank')
        await self.app(scope, receive, send)
