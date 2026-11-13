"""Presentation for independently configured cloud accounts, grouped by endpoint."""

from urllib.parse import urlsplit

SECTIONS = ('connection', 'access', 'models', 'limits')
PROVIDERS = {
    'openrouter.ai': ('OpenRouter', 'OR'), 'api.openai.com': ('OpenAI', 'OA'),
    'api.together.xyz': ('Together', 'TO'), 'api.groq.com': ('Groq', 'GQ'),
    'api.fireworks.ai': ('Fireworks', 'FW'),
}


def accounts_view(services, selected='', section='connection'):
    """Derive display facts without changing saved names, endpoints or credentials."""
    accounts, groups = [], {}
    for index, original in enumerate(services, 1):
        account = dict(original)
        try:
            host = urlsplit(account['url']).hostname or 'Custom endpoint'
        except ValueError:
            host = 'Custom endpoint'
        provider, mark = PROVIDERS.get(host, (host, 'API'))
        ceiling, spent = account.get('ceiling'), account.get('spent', 0)
        state = 'key' if not account.get('key') else 'limit' if ceiling and spent >= ceiling else 'ready'
        account.update(index=index, label=account['name'], provider=provider, mark=mark,
                       ceiling=ceiling, spent=spent, state=state,
                       status={'key': 'Needs API key', 'limit': 'Limit reached', 'ready': 'Key installed'}[state],
                       percent=min(100, spent / ceiling * 100) if ceiling else 0)
        accounts.append(account)
        groups.setdefault(provider, []).append(account)
    for account in accounts:
        others = [one['name'] for one in accounts if one['name'] != account['name'] and one['url'] == account['url']]
        account['endpoint_context'] = ('Also used by ' + ', '.join(others) + '. Changes here apply only to ' + account['name'] + '.'
            if others else 'Add another account at this endpoint whenever you need a different key or spending limit.')
    chosen = next((one for one in accounts if one['name'] == selected), accounts[0] if accounts else None)
    return dict(accounts=accounts, account_groups=groups, selected_account=chosen,
                section=section if section in SECTIONS else 'connection',
                endpoint_count=len({one['url'] for one in accounts}),
                model_count=sum(len(one.get('models', {})) for one in accounts),
                spent_total=sum(one['spent'] for one in accounts))
