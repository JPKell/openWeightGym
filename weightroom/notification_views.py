"""Shared write-only channel actions and catalogue-backed system/personal controls."""
import time
import uuid
from urllib.parse import quote

from starlette.responses import Response
from starlette.routing import Route

from loadcoach.setting_catalog import entries,get
from weightroom.notify import DeliveryError, account_owner


def context(notifications,program='weightroom',owner='system',choices=None,operational=False,refused=''):
    """Describe available delivery credentials and presence for the selected owner."""
    owner = account_owner(owner) if program=="account" and owner else owner
    definitions = entries(program)
    credentials = [entry for entry in definitions if entry.kind=='secret' and entry.name.startswith('notify_')]
    return {'notification_credentials':credentials,
            'notification_key_status':{entry.name:{**(notifications.keys.status(entry.name,owner) if owner else {'saved':False,'changed':None}),'refused':entry.name==refused} for entry in credentials},
            'notification_deliveries':notifications.status(owner) if owner else [],
            'notification_settings':[{'name':entry.name,'value':(choices or {}).get(entry.name,entry.default),
                                      'when':entry.tier,'show_key':False,'editor':'text' if entry.kind=='text' else '',
                                      'disabled':not owner or entry.name=='notify_operational' and not operational}
                                     for entry in definitions if entry.kind!='secret' and entry.name.startswith('notify_')] if program=='account' else []}


def routes(base,for_people,typed,go,notifications,render,note,*,personal=False):
    """Both desks use identical credential lifecycle, safe errors and test-send behavior."""
    landing = '/account/privacy' if personal else '/settings/alerts'
    program = 'account' if personal else 'weightroom'
    def allowed(person):
        """Permit personal delivery changes or authorized system settings management."""
        return personal or 'manage-settings' in person.can
    def owner(person):
        """Choose the stable personal credential scope or the system scope."""
        return account_owner(person.me['identity']) if personal else 'system'

    async def credential(request,person):
        """Authorize and validate a delivery credential action for its actual owner."""
        if personal and not person.me.get('identity'):
            return await render(request,person,'Sign in to an account before configuring delivery.',True)
        if not allowed(person):
            return Response('Notification settings require manage-settings.',status_code=403)
        name = request.path_params['kind']
        entry = get(program,name)
        if not entry or entry.kind!='secret' or not name.startswith('notify_'):
            return Response('Choose a notification credential.',status_code=404)
        form = await typed(request)
        operation = form.get('operation','save')
        try:
            if operation=='remove':
                notifications.keys.remove(name,owner(person))
            elif operation=='save':
                notifications.keys.save(name,form.get('credential',''),owner(person))
            else:
                raise ValueError('Choose save or remove.')
        except ValueError as problem:
            request.state.notification_refused = name
            return await render(request,person,str(problem),True)
        except OSError:
            request.state.notification_refused = name
            return await render(request,person,'The credential could not be saved. Check the control node key-folder permissions.',True)
        note('notification-credential',person,credential=name,operation=operation,personal=personal)
        return go(landing+'?said='+quote('Credential removed.' if operation=='remove' else 'Credential saved. Blank fields keep the current value.'))

    async def test(request,person):
        """Authorize and queue a test delivery using the current owner and settings."""
        if personal and not person.me.get('identity'):
            return await render(request,person,'Sign in to an account before configuring delivery.',True)
        if not allowed(person):
            return Response('Notification settings require manage-settings.',status_code=403)
        channel = request.path_params['channel']
        if channel not in (('ntfy','email') if personal else ('webhook','ntfy','email')):
            return Response('Choose a notification channel.',status_code=404)
        now = time.time()
        message = {'episode_id':'test-'+uuid.uuid4().hex,'condition':'notification-test','source':'Control node',
                   'title':getattr(notifications.settings(),'site_name','openWeightGym')+' notification test','state':'test','first_at':now,'last_at':now,'resolved_at':None,
                   'link':notifications.settings().notify_site_url or landing}
        failure = ''
        try:
            await notifications.sender.send(channel,message,person.me.get('choices',{}) if personal else None,owner(person))
        except DeliveryError as problem:
            failure = str(problem)
        except Exception as problem:
            failure = 'Test delivery failed ('+type(problem).__name__+').'
        note('notification-test',person,channel=channel,personal=personal,succeeded=not bool(failure))
        if failure:
            return await render(request,person,failure,True)
        return go(landing+'?said='+quote('Test '+channel+' notification sent.'))

    return [Route(base+'/credentials/{kind}',for_people(credential),methods=['POST']),
            Route(base+'/test/{channel}',for_people(test),methods=['POST'])]
