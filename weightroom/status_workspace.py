"""Caller-scoped current work for the Status dispatch workspace."""

from loadcoach.log import LEGACY  # the old default node, which rows and catalogues without a node name mean


def present(now, nodes, counts=None):
    live = now or {}
    batches = live.get('active_batches') or []
    grouped = {number for batch in batches for number in batch.get('request_ids', [])}
    processing = [{**item, 'description': 'being answered'} for item in live.get('running', [])
                  if item.get('id') not in grouped and not item.get('batch_id')]
    processing += [{**item, 'description': 'hosted', 'machine': 'Hosted'} for item in live.get('hosted', [])
                   if item.get('id') not in grouped and not item.get('batch_id')]
    processing += [{**item, 'batch': True} for item in batches]
    queued = [{**item, 'description': 'waiting'} for item in live.get('waiting', [])]
    for node in nodes:
        if now and len(nodes) == 1:  # one node: everything under way is its
            node['running'] = len(live.get('running', [])) + live.get('others_running', 0)
            node['waiting'] = len(live.get('waiting', [])) + live.get('others_waiting', 0)
        for model in node.get('loaded', []):
            matches = lambda item: item.get('model') == model['model'] and item.get('machine', LEGACY) == node['name']
            model['work'] = ('Unknown' if now is None else 'In use' if any(matches(item) for item in live.get('running', []))
                             else 'Queued' if any(matches(item) for item in live.get('waiting', [])) else 'Idle')
        node['status_search'] = ' '.join([node['name'], node.get('hostname') or '',
                                        *(model['model'] for model in node.get('loaded', []))]).lower()
    partial = any(not node.get('status_live') or node.get('state') != 'ready' for node in nodes)
    return dict(processing=processing, queued=queued,
                running=None if now is None else len(live.get('running', [])) + len(live.get('hosted', [])) + live.get('others_running', 0) + live.get('others_hosted', 0),
                waiting=None if now is None else len(queued) + live.get('others_waiting', 0),
                partial=partial, counts=(counts or {}).get('counts', [None, None, None]),
                count_scope=(counts or {}).get('scope', 'mine'),
                hidden=sum(live.get('others_' + key, 0) for key in ('running', 'waiting', 'hosted')))
