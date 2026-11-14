"""Select forwarded client addresses only through explicitly trusted proxy hops."""
from ipaddress import ip_address, ip_network


def client_address(peer, forwarded='', trusted=()):
    """Return the last untrusted hop; malformed or untrusted headers use the socket peer."""
    try:
        socket = ip_address(peer)
        networks = [ip_network(network, strict=False) for network in trusted]
        if not any(socket in network for network in networks) or not forwarded:
            return str(socket)
        parts = forwarded.split(',')
        if len(parts) > 32:
            return str(socket)
        hops = [ip_address(part.strip()) for part in parts]
        for hop in reversed(hops):
            if not any(hop in network for network in networks):
                return str(hop)
        return str(hops[0])
    except (ValueError, TypeError):
        return str(peer or 'unknown')[:100]


class ProxyAddress:
    """Apply the machine-file trust policy before request attribution is captured."""

    def __init__(self, app, trusted=()):
        """Bind the application and explicitly trusted proxy networks."""
        self.app, self.trusted = app, trusted

    async def __call__(self, scope, receive, send):
        """Resolve the request address only through explicitly trusted proxy headers."""
        if scope['type'] in ('http','websocket') and (peer := scope.get('client')):
            headers = dict(scope.get('headers', []))
            forwarded = headers.get(b'x-forwarded-for', b'').decode('latin-1')
            address = client_address(peer[0],forwarded,self.trusted)
            scope = {**scope, 'suite_socket_peer':peer[0], 'client':(address,peer[1])}
        return await self.app(scope,receive,send)
