"""Control-node remote MCP policy, write-only credentials and safe client retirement."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from starlette.responses import JSONResponse

from loadcoach.setting_catalog import REMOTE_SERVER_FIELDS, validate
from loadcoach.settings import Changes, live
from loadcoach.statement import user_of
from toollocker.servers import Remote, ToolProblem
from voice.telephone_keys import atomic_secret


class RemoteKeys:
    """Only named site credentials, never a machine-file server's credential path."""
    def __init__(self, folder):
        self.folder=Path(folder)

    def path(self,name):
        REMOTE_SERVER_FIELDS[0].check(name)
        return self.folder/('remote-mcp-'+hashlib.sha256(name.encode()).hexdigest()+'.key')

    def status(self,name):
        try:
            stamp=self.path(name).stat()
            return {'saved':True,'changed':datetime.fromtimestamp(stamp.st_mtime,UTC).isoformat()}
        except FileNotFoundError:
            return {'saved':False,'changed':None}

    def revision(self,name):
        try:
            stamp=self.path(name).stat()
            return stamp.st_ino,stamp.st_mtime_ns,stamp.st_size
        except FileNotFoundError:
            return None

    def save(self,name,value):
        path=self.path(name);validate('toollocker','remote_server_key',value)
        if value:atomic_secret(path,value)

    def read(self,name):
        try:
            value=self.path(name).read_text().strip()
            validate('toollocker','remote_server_key',value)
            return value
        except (FileNotFoundError,UnicodeError,ValueError):
            return ''

    def remove(self,name):
        self.path(name).unlink(missing_ok=True)


class ManagedRemote(Remote):
    """Sanitize reflected credentials and protocol failures before tools, logs or UI see them."""
    site_managed=True

    def safe(self,value):
        secret=self.headers.get('authorization','').removeprefix('Bearer ')
        def clean(item):
            if isinstance(item,str):return item.replace(secret,'(redacted)') if secret else item
            if isinstance(item,list):return [clean(one) for one in item]
            if isinstance(item,dict):return {clean(key):clean(one) for key,one in item.items()}
            return item
        return clean(value)

    async def start(self):
        try:
            await super().start()
        except (ToolProblem,ValueError,OSError):
            raise ToolProblem('The '+self.name+' remote MCP server is unavailable; check its address, saved key and service.') from None

    async def ask(self,method,params,timeout=30):
        try:
            return self.safe(await super().ask(method,params,timeout))
        except ToolProblem:
            raise ToolProblem('The '+self.name+' remote MCP request failed; check the remote service and credential.') from None


class RemoteSettings:
    """Reuse the normal setting store and audit contract; never save credentials in it."""
    def __init__(self,source,keys,base,files,log,changed):
        self.changes=Changes(source)
        self.keys,self.base,self.files,self.log,self.changed=keys,base,files,log,changed
        self.active={}
        self.runtime=lambda name,spec:'pending'
        self.retiring=set()

    def values(self):
        return live('toollocker','remote_servers',self.base,changes=self.changes)

    def status(self):
        overridden='remote_servers' in self.changes.of('toollocker')
        return {'servers':[{'name':name,**value,'credential':self.keys.status(name),
                            'shadowed':name in self.files(),'changed':overridden,
                            'state':'file' if name in self.files() else self.runtime(name,value)} for name,value in sorted(self.values().items())],
                'file_names':sorted(self.files()),'available':True}

    async def handle(self,request):
        """Inspect and change remote MCP servers using a signed superuser caller."""
        said=getattr(request.state,'said',None)
        user=user_of(said['caller']) if said and said.get('caller') else None
        if not user or not user.can('superuser'):
            return JSONResponse({'error':{'message':'Remote MCP management requires superuser.'}},403)
        if request.method=='GET':return JSONResponse(self.status(),headers={'cache-control':'no-store'})
        try:
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>65536:raise ValueError('Remote MCP requests may contain at most 64 KiB.')
            body=json.loads(raw)
            if not isinstance(body,dict) or body.keys()-{'operation','name','server','credential'}:
                raise ValueError('Send a remote server operation, name, configuration and optional credential.')
            name=body.get('name','');REMOTE_SERVER_FIELDS[0].check(name)
            operation=body.get('operation');previous=self.changes.of('toollocker').get('remote_servers')
            values=dict(self.values())
            credential=body.get('credential','');validate('toollocker','remote_server_key',credential)
            if operation=='save':
                server=body.get('server')
                validate('toollocker','remote_servers',{**values,name:server})
                if not server.get('public',False) and not credential and not self.keys.status(name)['saved']:
                    raise ValueError('Save a private server key, or choose a public HTTPS server.')
                if credential:self.keys.save(name,credential)
                values[name]=server
            elif operation=='remove':
                if name not in values:raise ValueError('No such site-managed remote server.')
                values.pop(name)
            elif operation=='save-key':
                self.keys.save(name,credential)
            elif operation=='remove-key':
                self.keys.remove(name)
            else:raise ValueError('Choose save, remove, save-key or remove-key.')
            if operation in ('save','remove'):
                self.changes.put('toollocker','remote_servers',values,user.name)
                self.log.add('settings','changed',user.name,program='toollocker',old={'remote_servers':previous},remote_servers=values)
            if operation in ('save-key','remove-key') or credential:
                self.log.add('settings','remote-credential',user.name,program='toollocker',server=name,operation=operation)
            self.changed.set()
            return JSONResponse(self.status(),headers={'cache-control':'no-store'})
        except (ValueError,TypeError) as problem:
            # Central contract errors contain no supplied values or credential material.
            return JSONResponse({'error':{'message':str(problem)}},400,headers={'cache-control':'no-store'})
        except OSError:
            return JSONResponse({'error':{'message':'The control node credential could not be written. Check key-folder permissions.'}},503)

    @asynccontextmanager
    async def using(self,server):
        if not getattr(server,'site_managed',False):
            yield
            return
        self.active[server]=self.active.get(server,0)+1
        try:yield
        finally:
            self.active[server]-=1

    def retire(self,server):
        async def close():
            while self.active.get(server,0):await asyncio.sleep(.05)
            try:await server.stop()
            finally:
                await server.client.aclose()
                self.active.pop(server,None)
        task=asyncio.create_task(close());self.retiring.add(task)
        def finished(task):
            self.retiring.discard(task)
            if not task.cancelled():task.exception()  # consume sanitized shutdown failures
        task.add_done_callback(finished)

    async def close(self):
        await asyncio.gather(*list(self.retiring),return_exceptions=True)
        self.changes.db.close()
