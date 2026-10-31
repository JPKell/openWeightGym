"""Fresh-process embedder readiness using the existing load measurement series."""
from . import load

DEFINITION = {
    **load.DEFINITION, 'id': 'embedding_load', 'name': 'Embedder load time', 'embedding': True,
    'summary': 'Measure how long a fresh embedding model process takes to become ready.',
    'method': load.DEFINITION['method'] + ' This series is only for embedding models; '
              'pooling and physical batch settings are preserved, and no vector is requested.',
}
PROTOCOL = {**load.PROTOCOL, 'version': 1, 'embedding': True}
measure = load.measure
summarize = load.summarize
