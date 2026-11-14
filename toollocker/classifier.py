"""Built-in Classifier MCP tool with the same caller-bound proof as extraction."""

from toollocker.extraction import ExtractionTools


class ClassifierTools(ExtractionTools):
    name, level = 'classifier', 'normal'
    path_prefix = '/owg/classifier-tools/'

    def __init__(self, url, key_file):
        super().__init__(url, key_file)
        self.tools = [{'name': 'classify', 'description': 'Answer typed choice, score and yes/no questions about text or JSON using a permitted Classifier model. Returns probabilities; they are not calibrated for your data.',
            'inputSchema': {'type': 'object', 'properties': {'model': {'type': 'string'},
                'state': {'type': ['string', 'object', 'array']}, 'questions': {'type': 'object', 'minProperties': 1, 'maxProperties': 64},
                'node': {'type': 'string'}}, 'required': ['model', 'state', 'questions'], 'additionalProperties': False}}]

    async def start(self):
        await super().start()
        import httpx
        self.client.timeout = httpx.Timeout(900)
