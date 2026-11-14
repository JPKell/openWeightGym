"""Bounded read-only docs tools delegate every permission decision to WeightRoom."""

import json

import httpx
from jsonschema import ValidationError, validate
from loadcoach import statement
from toollocker.extraction import ExtractionTools
from toollocker.servers import ToolProblem

READ_CHARS = 8000
SEARCH_LIMIT = 10
OPERATIONS = {
    'list_topics': 'catalogue', 'search': 'search', 'read': 'article',
    'find_endpoint': 'endpoints', 'lookup_setting': 'settings', 'explain_ability': 'abilities',
    'code_outline': 'source', 'code_symbol': 'source',
}


def descriptions():
    """Describe bounded query contracts without accepting arbitrary files or service URLs."""
    query = {'type': 'string', 'maxLength': 300}
    definitions = [
        ('list_topics', 'List the documentation topics visible to your account, with citation URLs.', {}, []),
        ('search', 'Search permitted guides and references before answering. Results carry canonical citation URLs.',
         {'q': query, 'scope': {'type': 'string', 'maxLength': 80}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': SEARCH_LIMIT}}, ['q']),
        ('read', 'Read a permitted canonical /docs article or heading as bounded plain text. Follow next_offset to continue.',
         {'url': {'type': 'string', 'maxLength': 300}, 'anchor': {'type': 'string', 'maxLength': 200}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2000000}}, ['url']),
        ('find_endpoint', 'Find declared API operations and source access checks. Administrator reference.', {'q': query}, []),
        ('lookup_setting', 'Find shipped setting defaults and meanings. Never reads live settings. Administrator reference.', {'q': query}, []),
        ('explain_ability', 'Find ability definitions and their meaning. Administrator reference.', {'q': query}, []),
        ('code_outline', 'Read an allowlisted module outline and its documented symbols. Administrator reference.',
         {'module': {'type': 'string', 'maxLength': 200}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2000000}}, ['module']),
        ('code_symbol', 'Read one exact documented symbol in an allowlisted module. Administrator reference.',
         {'module': {'type': 'string', 'maxLength': 200}, 'symbol': {'type': 'string', 'maxLength': 200}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2000000}}, ['module', 'symbol']),
    ]
    return [{'name': name, 'description': description, 'inputSchema': {'type': 'object', 'properties': properties,
             'required': required, 'additionalProperties': False}} for name, description, properties, required in definitions]


class DocsTools(ExtractionTools):
    """Use signed caller proof for every read; the adapter never reads documentation files."""
    name, level = 'docs', 'normal'

    def __init__(self, url, key_file):
        """Bind the console's private endpoint and the existing machine signing key."""
        super().__init__(url, key_file)
        self.tools = descriptions()

    async def call_for(self, user, tool, arguments, level):
        """Validate a bounded query, sign its actual caller and require the console's proof."""
        descriptor = next((entry for entry in self.tools if entry['name'] == tool), None)
        try:
            if not descriptor:
                raise ValueError('Unknown documentation tool.')
            validate(arguments, descriptor['inputSchema'])
            request = self.client.build_request('GET', '/owg/docs/' + OPERATIONS[tool], params=arguments)
            key = statement.read_key(self.key_file)
            where = request.url.raw_path.decode()
            header, nonce = statement.make(key, 'GET', where, b'', caller=statement.caller_of(user), purpose='docs-tool')
            request.headers[statement.HEADER] = header
            response = await self.client.send(request)
            if response.headers.get(statement.PROOF) != statement.proof(key, nonce):
                raise ValueError('Documentation gateway did not prove its identity.')
            if not response.is_success:
                raise ValueError(response.json().get('error', {}).get('message', 'Documentation was refused.'))
            return {'content': [{'type': 'text', 'text': json.dumps(response.json(), ensure_ascii=False)}], 'isError': False}
        except (httpx.HTTPError, OSError):
            raise ToolProblem('Documentation gateway is unavailable.') from None
        except (ValueError, ValidationError) as error:
            raise ToolProblem(str(error)) from None
