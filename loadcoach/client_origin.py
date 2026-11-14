"""Authenticated browser-address forwarding across the control node's internal HTTP hop."""

from contextvars import ContextVar
from ipaddress import ip_address

from loadcoach import statement

HEADER = 'x-suite-client-origin'
CURRENT = ContextVar('suite_client_address', default='')


async def forward(request, key: str, machine: str = 'weightroom') -> None:
    """Bind a client address to the method, URL and bearer credential without buffering uploads."""
    request.headers.pop(HEADER, None)
    address = CURRENT.get()
    if address and key:
        signed, _ = statement.make(key, request.method, request.url.raw_path.decode('ascii'),
                                   request.headers.get('authorization', '').encode(), machine=machine,
                                   origin=address, purpose='client-origin')
        request.headers[HEADER] = signed


class ClientOrigin:
    """Capture socket addresses, accepting a forwarded address only with the private control node proof."""

    def __init__(self, app, key_file: str = '') -> None:
        """Optionally configure proof verification; a console without a key file captures its own peer."""
        self.app, self.key_file = app, key_file
        self.nonces = statement.Nonces()

    async def __call__(self, scope, receive, send):
        """Scope the observed address to this request and its child tasks, never another caller."""
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        client = scope.get('client')
        address = client[0] if client else ''
        forwarded = headers.get(HEADER.encode())
        if forwarded and self.key_file:
            where = statement.address((scope.get('raw_path') or scope['path'].encode()).decode(),
                                      scope.get('query_string', b''))
            try:
                said = statement.check(statement.read_key(self.key_file), forwarded.decode(), scope.get('method', 'GET'),
                                       where, headers.get(b'authorization', b''), self.nonces)
                if said.get('machine') in ('weightroom', 'voice-gateway') and said.get('purpose') == 'client-origin':
                    address = str(ip_address(said['origin']))
                    from loadcoach.observability import adopt
                    adopt(said.get('observation'))
                    scope = {**scope, 'client': (address, 0)}
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                pass  # An untrusted forwarding header never replaces the actual peer.
        if forwarded:
            scope = {**scope, 'headers': [(k, v) for k, v in scope['headers'] if k != HEADER.encode()]}
        try:
            address = str(ip_address(address))
        except ValueError:
            address = ''
        token = CURRENT.set(address)
        try:
            return await self.app(scope, receive, send)
        finally:
            CURRENT.reset(token)
