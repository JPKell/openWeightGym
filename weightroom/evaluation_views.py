"""Presentation for owner-authorized collection inventories and workflow runs."""


def collection_summary(collection):
    """Derive readiness from actual source states, without starting inference."""
    documents = collection.get('documents', [])
    ready = sum(doc['state'] == 'ready' for doc in documents)
    indexing = sum(doc['state'] == 'indexing' for doc in documents)
    failed = sum(doc['state'] == 'failed' for doc in documents)
    pending = len(documents) - ready - indexing - failed
    if failed:
        state, label, tone = 'attention', 'Needs attention', 'orange'
    elif indexing:
        state, label, tone = 'indexing', 'Indexing', 'azure'
    elif pending:
        state, label, tone = 'pending', 'Awaiting indexing', 'secondary'
    elif ready:
        state, label, tone = 'ready', 'Ready to retrieve', 'success'
    else:
        state, label, tone = 'empty', 'Add sources', 'secondary'
    return {**collection, 'state': state, 'state_label': label, 'tone': tone,
            'document_count': len(documents), 'ready_count': ready, 'indexing_count': indexing,
            'failed_count': failed, 'pending_count': pending,
            'chunk_count': sum(doc.get('chunks', 0) for doc in documents)}


def collection_workspace(collections, query='', state=''):
    """Keep the full permitted inventory while marking the requested filter scope."""
    rows = [collection_summary(c) for c in collections]
    for row in rows:
        row['search'] = ' '.join(str(row.get(key, '')) for key in ('title', 'model_label', 'level')).casefold()
        row['matches'] = (not state or row['state'] == state) and query.strip().casefold() in row['search']
    return dict(rows=rows, count=len(rows), visible=sum(row['matches'] for row in rows),
                sources=sum(row['document_count'] for row in rows),
                ready=sum(row['state'] == 'ready' for row in rows),
                attention=sum(row['failed_count'] > 0 for row in rows))


def workflow_summary(run, collections):
    """Map every persisted execution state to an honest label and next action."""
    state = run['state']
    stage = 'draft' if state.endswith('draft') or state == 'drafting' else 'research'
    if state == 'completed':
        group, label, action, tone, step = 'completed', 'Completed', 'Review accepted output', 'success', 4
    elif state == 'no_evidence':
        group, label, action, tone, step = 'attention', 'No matching evidence', 'Inspect collection', 'orange', 0
    elif state in ('researching', 'drafting'):
        group, label, action, tone, step = 'running', stage.capitalize() + ' in progress', 'Refresh status', 'azure', 2 if stage == 'draft' else 0
    elif state.startswith('review_'):
        group, label, action, tone, step = 'review', 'Review ' + stage, 'Accept ' + stage, 'orange', 3 if stage == 'draft' else 1
    elif state.startswith(('failed_', 'paused_')):
        group, label, action, tone, step = 'attention', stage.capitalize() + (' paused' if state.startswith('paused_') else ' needs retry'), 'Resume / retry ' + stage, 'orange', 2 if stage == 'draft' else 0
    else:
        group, label, action, tone, step = 'ready', 'Ready for ' + stage, 'Start ' + stage, 'secondary', 2 if stage == 'draft' else 0
    source = next((c for c in collections if c['id'] == run['collection']), None)
    return {**run, 'group': group, 'state_label': label, 'next_action': action, 'tone': tone, 'step': step,
            'collection_title': source['title'] if source else 'Collection unavailable',
            'research_accepted': state in ('ready_draft', 'drafting', 'review_draft', 'failed_draft', 'paused_draft', 'completed')}


def workflow_workspace(runs, collections, query='', state=''):
    """Count retained runs separately from the filtered navigator."""
    rows = [workflow_summary(run, collections) for run in runs]
    for row in rows:
        row['search'] = ' '.join([row['title'], row['question'], row['collection_title'],
                                  *(model.get('id', '') for model in row.get('config', {}).get('models', {}).values())]).casefold()
        row['matches'] = (not state or row['group'] == state) and query.strip().casefold() in row['search']
    return dict(rows=rows, visible=sum(row['matches'] for row in rows), count=len(rows),
                review=sum(row['group'] == 'review' for row in rows),
                attention=sum(row['group'] == 'attention' for row in rows),
                completed=sum(row['group'] == 'completed' for row in rows))
