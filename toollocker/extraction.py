"""Built-in extraction tools use trusted per-request identity, never a shared user API key."""

import asyncio
import json

import httpx

from loadcoach import statement

PRESETS = {'describe_image': 'describe', 'extract_image_text': 'text', 'extract_receipt': 'receipt',
           'extract_image_table': 'table', 'extract_image_fields': 'custom'}


def descriptions():
    """Describe the built-in task, status and cancellation MCP contracts."""
    tools = []
    for name, task in PRESETS.items():
        fields = {'asset_id': {'type': 'string', 'description': 'An image asset ID uploaded by this caller to /owg/extraction-assets.'},
                  'model': {'type': 'string', 'description': 'A permitted vision model or role with JSON Schema support.'},
                  'output_format': {'type': 'string', 'enum': ['plain', 'layout', 'markdown', 'json', 'csv']},
                  'instructions': {'type': 'string'}, 'temporary': {'type': 'boolean'}}
        required = ['asset_id', 'model']
        if task == 'custom':
            fields['schema_json'] = {'type': 'string', 'description': 'A closed nullable JSON Schema encoded as a JSON string.'}
            required.append('schema_json')
        if task not in ('receipt', 'table'):
            fields['output_format']['enum'].remove('csv')
        if task == 'describe':
            fields['detail'] = {'type': 'string', 'enum': ['concise', 'detailed']}
        tools.append({'name': name, 'description': f'Start {task} extraction from your uploaded image. Returns a private job ID; use extraction_status to read its result. No image paths or remote URLs.',
                      'inputSchema': {'type': 'object', 'properties': fields, 'required': required, 'additionalProperties': False}})
    for name, description in [('extraction_status', 'Read your extraction status and a bounded result, including authenticated export paths.'),
                               ('cancel_extraction', 'Cancel your queued or running extraction. Completed results are retained.')]:
        tools.append({'name': name, 'description': description, 'inputSchema': {'type': 'object',
            'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}})
    return tools


class ExtractionTools:
    """Delegate extraction using a signed proof of the actual tool caller."""
    name, level = 'extraction', 'normal'
    path_prefix = '/owg/extraction-tools/'

    def __init__(self, url, key_file):
        """Prepare a front gateway connection without a shared user key."""
        self.url, self.key_file = url.rstrip('/'), key_file
        self.starting = asyncio.Lock()
        self.tools = descriptions()
        self.client = None

    def dead(self):
        """Report whether the adapter needs its HTTP client started."""
        return self.client is None

    async def start(self):
        """Open the bounded internal gateway client."""
        self.client = httpx.AsyncClient(base_url=self.url, timeout=30)

    async def stop(self):
        """Close the gateway connection during ToolLocker shutdown."""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def call_for(self, user, tool, arguments, level):
        """Validate arguments, sign the caller and require gateway identity proof."""
        from toollocker.servers import ToolProblem
        from jsonschema import validate, ValidationError
        descriptor = next((entry for entry in self.tools if entry['name'] == tool), None)
        try:
            if not descriptor:
                raise ValueError('Unknown extraction tool.')
            validate(arguments, descriptor['inputSchema'])
            path = self.path_prefix + tool
            body = json.dumps({'arguments': arguments, 'sensitivity': level}).encode()
            header, nonce = statement.make(statement.read_key(self.key_file), 'POST', path, body,
                                           caller=statement.caller_of(user), purpose='extraction-tool')
            response = await self.client.post(path, content=body, headers={statement.HEADER: header, 'content-type': 'application/json'})
            if response.headers.get(statement.PROOF) != statement.proof(statement.read_key(self.key_file), nonce):
                raise ValueError('Extraction gateway did not prove its identity.')
            if not response.is_success:
                raise ValueError(response.json().get('error', {}).get('message', 'Extraction was refused.'))
            return {'content': [{'type': 'text', 'text': json.dumps(response.json(), ensure_ascii=False)}], 'isError': False}
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            raise ToolProblem(str(error) if not isinstance(error, httpx.HTTPError) else 'Extraction gateway is unavailable.') from None
