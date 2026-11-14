"""Superuser-only native remote MCP forms through the normal control node ToolLocker."""
from urllib.parse import quote

import httpx
from starlette.responses import Response
from starlette.routing import Route

from loadcoach.setting_catalog import REMOTE_SERVER_FIELDS, get, validate


def fields():
    """Return the canonical remote-server editor contracts."""
    return REMOTE_SERVER_FIELDS


async def context(client,person):
    """Read sanitized remote-server status only for a superuser."""
    if 'superuser' not in person.can:return {'remote_admin':False}
    try:
        answer=await client.get('/remote-servers',headers={'authorization':'Bearer '+person.key},timeout=10)
        if answer.is_success:
            return {'remote_admin':True,'remote_status':answer.json(),'remote_fields':fields(),
                    'remote_credential':get('toollocker','remote_server_key'),'remote_contract':get('toollocker','remote_servers')}
    except (httpx.HTTPError,ValueError):pass
    return {'remote_admin':True,'remote_status':{'available':False,'servers':[]},'remote_fields':fields(),
            'remote_credential':get('toollocker','remote_server_key'),'remote_contract':get('toollocker','remote_servers')}


def routes(for_people,client,typed,render,go):
    """Register caller-authenticated remote-server settings actions."""
    async def save(request,person):
        """Require superuser access before forwarding a validated remote-server action."""
        if 'superuser' not in person.can:return Response('Remote MCP management requires superuser.',status_code=403)
        form=await typed(request)
        name=request.path_params.get('server') or form.get('name','')
        operation=form.get('operation','save')
        server={field.name:(form.get(field.name)=='on' if field.kind=='bool' else form.get(field.name,field.default))
                for field in REMOTE_SERVER_FIELDS[1:]}
        try:
            REMOTE_SERVER_FIELDS[0].check(name)
            if operation=='save':validate('toollocker','remote_servers',{name:server})
            answer=await client.post('/remote-servers',headers={'authorization':'Bearer '+person.key},
                json={'operation':operation,'name':name,'server':server,'credential':form.get('credential','')},timeout=15)
            if not answer.is_success:
                return await render(request,person,answer.json()['error']['message'],failed=True,status=answer.status_code)
        except ValueError as problem:
            # A rejected credential-bearing URL must never be refilled into a public text field.
            values=getattr(request.state,'form_values',None)
            if isinstance(values,dict):values.pop('url',None)
            return await render(request,person,str(problem),failed=True)
        except (httpx.HTTPError,KeyError):
            return await render(request,person,'The control node tool service is unavailable. No change was queued.',failed=True,status=503)
        return go('/tools?said='+quote('Remote server updated.' if operation in ('save','remove') else 'Remote credential updated. Blank keeps the saved key.'))
    return [Route('/tools/remotes',for_people(save),methods=['POST']),
            Route('/tools/remotes/{server}',for_people(save),methods=['POST'])]
