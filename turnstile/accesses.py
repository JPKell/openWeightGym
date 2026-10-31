"""Record inbound key use against the verified client peer without retaining credentials."""

from ipaddress import ip_address


class ClientAccesses:
    """Observe each external bearer-key attempt once, after trusted origin resolution."""

    def __init__(self, app, users, log, internal_prefix: str = '') -> None:
        """Use front's existing account store and audit writer; exclude private in-process dispatch."""
        self.app, self.users, self.log = app, users, log
        self.internal_prefix = internal_prefix

    async def __call__(self, scope, receive, send):
        """Count valid or rejected keys by IP and stable account, never logging the key itself."""
        address, recorded = '', False
        if scope['type'] == 'http':
            headers = dict(scope.get('headers', []))
            scheme, _, key = headers.get(b'authorization', b'').decode('latin-1').partition(' ')
            client = scope.get('client')
            try:
                address = str(ip_address(client[0])) if client else ''
            except ValueError:
                address = ''
            recorded = bool(self.internal_prefix and key.startswith(self.internal_prefix))
            if (address and scheme.lower() == 'bearer' and key
                    and not recorded):
                user = self.users.by_key(key)
                _, name = self.users.record_access(address, key, user)
                recorded = True
                if user is None:
                    self.log.add('key', 'rejected', name, address, path=scope['path'],
                                 why='Invalid, disabled or expired API key.')
        async def observed(message):
            """Also retain rejected requests which supplied no usable bearer credential."""
            nonlocal recorded
            if address and not recorded and message['type'] == 'http.response.start' and message['status'] in (401, 403):
                self.users.record_access(address, '', None)
                self.log.add('key', 'rejected', '', address, path=scope['path'], why='Missing or unsupported API key.')
                recorded = True
            await send(message)

        return await self.app(scope, receive, observed)
