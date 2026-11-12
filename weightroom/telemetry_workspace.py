"""Presentation of physical-host telemetry; missing or stale readings stay unknown."""

import math
import re
from urllib.parse import quote

from loadcoach.gpu_type import is_integrated


def number(value):
    """Accept finite sensor numbers, including zero, but never manufacture missing data."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def percentage(value, total):
    """A bounded meter value, or unknown when either side is unavailable."""
    value, total = number(value), number(total)
    return max(0, min(100, value / total * 100)) if value is not None and total and total > 0 else None


def component_pills(host, inventory):
    """Select installed Components entries for this physical host, preserving conflicting node states."""
    from loadcoach.components import everything
    catalogue = everything()
    nodes = [node for node in inventory.get('nodes', []) if node.get('name') in host.get('machines', [])]
    if any(node.get('with_front') for node in nodes):
        nodes += [node for node in inventory.get('nodes', []) if node.get('name') == 'front' and node not in nodes]
    labels = {'running': ('Running', 'green'), 'stopped': ('Stopped', 'secondary'),
              'failed': ('Failed', 'red'), 'starting': ('Starting', 'blue'),
              'stopping': ('Stopping', 'yellow'), 'reloading': ('Reloading', 'blue'),
              'masked': ('Masked', 'secondary')}
    rows, seen = [], set()
    for node in nodes:
        if not node.get('answering'):
            continue
        for kind in ('services', 'engines'):
            allowed = {row['id']: row for row in catalogue[kind]}
            for source in node.get(kind, []):
                identity = source.get('id')
                if identity not in allowed or source.get('installed') is not True:
                    continue
                if kind == 'services':
                    label, tone = labels.get(source.get('state'), ('Unknown', 'secondary'))
                    if source.get('state') == 'running' and source.get('detail') == 'exited':
                        label, tone = 'Not running', 'secondary'
                else:
                    label, tone = (('Ready', 'green') if source.get('available') else
                                   ('Turned off', 'secondary') if not source.get('enabled') else ('Not usable', 'red'))
                key = (kind, identity, label, tone, source.get('unit', ''))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({'id': identity, 'kind': kind, 'node': node['name'],
                             'name': identity if kind == 'services' else allowed[identity]['label'],
                             'unit': source.get('unit') or source.get('why') or identity,
                             'state': source.get('state', ''), 'label': label, 'tone': tone})
    for row in rows:
        if sum((one['kind'], one['id']) == (row['kind'], row['id']) for one in rows) > 1:
            row['name'] += ' at ' + row['node']
    return rows if any(node.get('answering') for node in nodes) else None


def hosts_view(hosts, poll):
    """Add safe current readings to the already deduplicated physical-host inventory."""
    result = []
    for source in hosts:
        host = dict(source)
        snapshot = source.get('telemetry') or {}
        age = number(source.get('age'))
        fresh = bool(snapshot and age is not None and age <= max(5, poll) * 3 and source.get('state') == 'ready')
        current = lambda value: number(value) if fresh else None
        uptime = current(snapshot.get('up_for'))
        memory, processor = snapshot.get('memory') or {}, snapshot.get('processor') or {}
        cards = [{**gpu, 'card': gpu['card'], 'model': gpu.get('model') or 'GPU',
                  **{key: current(gpu.get(key)) for key in ('load', 'memory_used', 'temperature', 'power', 'fan')},
                  'memory_total': number(gpu.get('memory_total')), 'power_limit': number(gpu.get('power_limit'))}
                 for gpu in snapshot.get('cards', []) if isinstance(gpu.get('card'), int) and not is_integrated(gpu)]
        disks = []
        for disk in snapshot.get('disks', []):
            total, free = number(disk.get('total')), number(disk.get('free'))
            disks.append({**disk, 'total': total, 'free': current(free),
                          'used': current(max(0, total - free)) if total is not None and free is not None else None})
        complete_disks = bool(disks) and all(d['total'] is not None and d['used'] is not None for d in disks)
        used = sum(d['used'] for d in disks) if complete_disks else None
        capacity = sum(d['total'] for d in disks) if complete_disks else None
        identity = source.get('fingerprint') or 'unidentified:' + ','.join(source.get('machines', []))
        host.update(identity=identity, fresh=fresh, age=age, has_inventory=bool(snapshot),
                    uptime=uptime if uptime is not None and uptime >= 0 else None,
                    gpu_inventory_known=isinstance(snapshot.get('cards'), list), gpus=cards, disks=disks,
                    excluded_gpu_ids=[*snapshot.get('excluded_gpu_ids', []),
                                      *(gpu['card'] for gpu in snapshot.get('cards', []) if is_integrated(gpu))],
                    cpu=current(processor.get('load')), temperature=current(processor.get('temperature')),
                    ram=current(memory.get('used')), ram_total=number(memory.get('total')),
                    disk_percent=percentage(used, capacity),
                    label='Reporting' if fresh else ('Stale' if snapshot else 'No readings') if source.get('state') == 'ready'
                    else {'not answering': 'Offline', 'sleeping': 'Sleeping', 'unknown': 'Unknown'}.get(source.get('state'), 'Unknown'),
                    href='/telemetry/' + source['fingerprint'] if source.get('fingerprint') else '/telemetry?node=' + quote(identity, safe=''))
        result.append(host)
    return sorted(result, key=lambda host: (host.get('hostname', '').casefold(), host['identity']))


def fleet_summary(hosts):
    """Count physical devices once and distinguish unknown inventory from zero devices."""
    cards = [gpu for host in hosts for gpu in host['gpus']]
    return dict(total=len(hosts), reporting=sum(host['fresh'] for host in hosts), gpus=len(cards),
                vram=sum(gpu['memory_total'] or 0 for gpu in cards),
                partial=any(not host['gpu_inventory_known'] for host in hosts) or any(gpu['memory_total'] is None for gpu in cards),
                attention=sum(not host['fresh'] for host in hosts))


def history_groups(host, past, chart):
    """Offer every device/metric, retaining gaps and the existing green-to-red thresholds."""
    series = past.get('series') or {}
    excluded = set(host.get('excluded_gpu_ids', []))
    gpu_ids = {gpu['card'] for gpu in host['gpus']} - excluded
    gpu_ids.update(int(match[1]) for name in series if (match := re.match(r'^GPU (\d+) ', name)) and int(match[1]) not in excluded)
    by_id = {gpu['card']: gpu for gpu in host['gpus']}
    for gpu_id in sorted(gpu_ids - by_id.keys()):
        gpu = dict(card=gpu_id, model='Retained device', memory_total=None, power_limit=None,
                   load=None, memory_used=None, temperature=None, power=None, fan=None)
        host['gpus'].append(gpu)
        by_id[gpu_id] = gpu
    host['gpus'].sort(key=lambda gpu: gpu['card'])
    groups = []

    def one(name, title, maximum=None, current=None, values=None):
        raw = series.get(name, []) if values is None else values
        raw = [number(value) for value in raw]
        item = chart(name, raw, maximum) | {'title': title, 'current': current}
        # Even without samples, expose the sensor and explain its empty history.
        item['unit'] = ' GB' if 'memory' in name or name.startswith('free on') else '°' if 'temperature' in name else ' W' if 'power' in name else '%'
        item['current_value'] = current / 2**30 if current is not None and item['unit'] == ' GB' else current
        return item

    groups.append(dict(id='processor', title='Processor & memory', charts=[
        one('processor load', 'Processor load', 100, host['cpu']),
        one('processor temperature', 'Processor temperature', None, host['temperature']),
        one('memory used', 'Memory used', host['ram_total'], host['ram'])]))
    for gpu_id in sorted(gpu_ids):
        gpu = by_id[gpu_id]
        metrics = [('memory used', 'memory_used', gpu['memory_total']), ('load', 'load', 100),
                   ('temperature', 'temperature', None), ('power', 'power', gpu['power_limit']), ('fan', 'fan', 100)]
        groups.append(dict(id=f'gpu-{gpu_id}', title=f'GPU {gpu_id}', charts=[
            one(f'GPU {gpu_id} {metric}', metric.capitalize(), top, gpu.get(key)) for metric, key, top in metrics]))
    paths = {disk['path']: disk for disk in host['disks']}
    paths.update({name.removeprefix('free on '): {'total': None, 'used': None} for name in series
                  if name.startswith('free on ') and name.removeprefix('free on ') not in paths})
    storage = []
    for path, disk in paths.items():
        capacity = disk.get('total')
        if capacity is not None:
            values = [None if number(value) is None else max(0, capacity - value) for value in series.get('free on ' + path, [])]
            storage.append(one('memory used on ' + path, 'Used on ' + path, capacity, disk.get('used'), values))
        else:
            storage.append(one('free on ' + path, 'Free on ' + path))
    if storage:
        groups.append(dict(id='storage', title='Storage', charts=storage))
    return groups
