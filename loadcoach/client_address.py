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
