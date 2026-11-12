"""Presentation of destinations on an individual artifact's page."""

from turnstile.library import identity


def destinations(file: dict, found: dict) -> list[dict]:
    """Match receiver place selection and distinguish known copies from stale inventories."""
    made = []
    source_place = file.get('place', '').removeprefix('storage_')
    for node in found.get('nodes', []):
        if node['name'] == found.get('artifact_machine', 'front'):
            continue
        places = node.get('places') or []
        place = next((one for one in places if one['name'] == source_place), places[0] if places else {})
        answering = bool(node.get('answering'))
        present = answering and any(identity(one) == identity(file) for one in node.get('files', []))
        made.append(dict(name=node['name'], answering=answering, available=answering and bool(places),
                         present=present, free=place.get('free') if answering else None,
                         place=place.get('name', ''), checked=node.get('checked')))
    return made
