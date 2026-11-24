"""Presentation of destinations on an individual artifact's page."""

from loadcoach.config import kind_of
from turnstile.library import identity, place_for


def destinations(file: dict, found: dict) -> list[dict]:
    """Match receiver place selection and distinguish known copies from stale inventories."""
    made = []
    kind = kind_of(file.get('place', ''))
    for node in found.get('nodes', []):
        if node['name'] == found.get('artifact_machine', 'front'):
            continue
        places = node.get('places') or []
        place = place_for(node, kind) or {}
        answering = bool(node.get('answering'))
        present = answering and any(identity(one) == identity(file) for one in node.get('files', []))
        made.append(dict(name=node['name'], answering=answering, available=answering and bool(places),
                         present=present, free=place.get('free') if answering else None,
                         place=place.get('name', ''), checked=node.get('checked')))
    return made
