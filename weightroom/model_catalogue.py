"""Presentation of verified Models inventory; every node retains its own configuration."""

from urllib.parse import quote, urlencode


def editor_address(name, engine, node):
    """Open the correct editor with this copy's local model name and compute node."""
    workspace = {'audiocpp': 'audio', 'sdcpp': 'images'}.get(engine)
    if workspace:
        return '/models/' + workspace + '?' + urlencode({'model': name, 'node': node}, quote_via=quote)
    return '/models/' + quote(name, safe='/:') + '?node=' + quote(node, safe='')


def catalogue(known, node=''):
    """Summarize authorized copies without treating the first copy's state as fleet-wide."""
    rows, machines = [], set()
    offline = set(known.get('unavailable_machines') or [])
    for model in known.get('models', []):
        copies = model.get('locations') or [{**model, 'machine': node or 'compute'}]
        placements = []
        for copy in copies:
            machine = copy['machine']
            if copy.get('unavailable') or machine in offline:
                state = 'offline'
            elif copy.get('size') is None:
                state = 'missing'
            elif copy.get('running'):
                state = 'in-use'
            elif copy.get('loaded') == 'ready':
                state = 'loaded'
            elif copy.get('loaded'):
                state = 'loading'
            elif copy.get('enabled') is False:
                state = 'disabled'
            else:
                state = 'installed'
            placements.append(dict(copy=copy, node=machine, state=state,
                disabled=copy.get('enabled') is False,
                label={'offline':'Node unavailable', 'missing':'File missing', 'disabled':'Placement disabled',
                       'in-use':'In use', 'loaded':'Loaded', 'loading':'Loading', 'installed':'Installed'}[state] +
                      (' · placement disabled' if copy.get('enabled') is False and state != 'disabled' else ''),
                href=editor_address(copy['name'], copy.get('engine', model.get('engine')), machine)))
            machines.add(machine)
        contexts = sorted({copy.get('flags', {}).get('ctx-size') for copy in copies
                           if isinstance(copy.get('flags', {}).get('ctx-size'), int) and copy['flags']['ctx-size'] > 0})
        sizes = sorted({copy['size'] for copy in copies if isinstance(copy.get('size'), (int, float))})
        preferred = next((place for place in placements if place['node'] == node), placements[0])
        rows.append(dict(model=model, href=preferred['href'], placements=placements, contexts=contexts, sizes=sizes,
            active=sum(p['state'] in ('loaded', 'in-use') for p in placements),
            in_use=sum(p['state'] == 'in-use' for p in placements),
            search=' '.join(str(value) for value in [model['name'], model.get('engine', 'llama_cpp'),
                *(p['node'] for p in placements), *(p['copy']['name'] for p in placements),
                model.get('sdcpp', {}).get('family', ''), model.get('audiocpp', {}).get('family', '')]).lower()))
    placements = [p for row in rows for p in row['placements']]
    return dict(rows=rows, installations=len(placements), nodes=len(machines),
                active=sum(row['active'] for row in rows), in_use=sum(row['in_use'] for row in rows),
                offline=sorted(offline | {p['node'] for p in placements if p['state'] == 'offline'}),
                links={(p['node'], p['copy']['name']): p['href'] for p in placements})
