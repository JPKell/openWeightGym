"""Owner-private Tool chat using the existing chat tasks, store and caller-key tool loop."""

import asyncio
from fnmatch import fnmatchcase
import json
import time

import httpx
from starlette.responses import JSONResponse

from loadcoach.groups import LEVELS
from turnstile.extractions import PRIVATE
from turnstile.tool_loop import ToolClient, ToolTurn, ToolLimit, model_result, exact_result, TOOL_PATH
from weightroom.chat_store import trace_of
from weightroom.extraction import Problem


def failure(status, message, code='tool_chat_error'):
    """Use the normal private error envelope for console API operations."""
    return JSONResponse({'error': {'message': message, 'type': 'tool_chat_error', 'code': code}}, status,
                        headers=PRIVATE)


class ToolChat:
    """Share conversation persistence and task ownership while pausing for explicit approvals."""
    def __init__(self, chats, turns, turn_type, compute, inference, tools, *, level_reader=None, settings=lambda: {}, toolsets=lambda: []):
        """Bind existing clients and settings; no service identity or extra listener is created."""
        self.chats, self.turns, self.turn_type = chats, turns, turn_type
        self.compute, self.inference, self.tools = compute, inference, tools
        self.level_reader, self.settings, self.toolsets = level_reader, settings, toolsets

    def client(self, request, user, number=0, level='normal', toolset=''):
        """Build a caller-key client for exactly one existing MCP route."""
        if not isinstance(toolset,str):
            raise Problem('Choose a configured tool set.')
        path='/mcp'+('/'+toolset if toolset else '')
        if not TOOL_PATH.fullmatch(path):
            raise Problem('Choose a configured tool set.')
        reader=(lambda trace:self.level_reader(user.info['name'],trace)) if self.level_reader else None
        return ToolClient(self.tools,path,request.headers.get('authorization',''),trace_of(number) if number else '',level,reader)

    async def allowed(self, request, user, number=0, level='normal', toolset=''):
        """Check current ability and current caller-visible tools on every operation."""
        if not user.can('use-tools'):
            raise Problem('Your account may not use tools.',403,'tools_not_allowed')
        client=self.client(request,user,number,level,toolset)
        await client.listing()
        if not client.tools:
            raise Problem('No ToolLocker tools are visible to your account.',403,'tools_not_allowed')
        return client

    def talk(self, request, user):
        """Hide another owner, another kind, or a record above current clearance with one 404."""
        ref=request.path_params.get('ref','')
        talk=self.chats.get(int(ref),user.name) if ref.isdigit() else None
        if (not talk or talk['kind']!='tools' or LEVELS.index(talk['level'])>LEVELS.index(user.info.get('max_sensitivity','normal'))):
            raise Problem('You have no such conversation.',404,'not_found')
        return talk

    def shown(self, talk):
        """Expose exact stored messages and task state without exposing credentials."""
        ref=str(talk['id'])
        return {**talk,'id':ref,'pending':bool(ref in self.turns)}

    async def catalogue(self, request, user):
        """List only the caller's tools, with names of narrowing configured sets."""
        client=await self.allowed(request,user,toolset=request.query_params.get('toolset',''))
        sets=self.toolsets()
        names=[name for name, config in sets.items() if any(fnmatchcase(tool['name'], pattern) for tool in client.tools for pattern in config.get('tools', []))] if isinstance(sets,dict) else list(sets)
        return JSONResponse({'tools':client.tools,'toolsets':names})

    async def listing(self, request, user):
        """Keep Tool chat history separate from Text chat and filter current clearance."""
        await self.allowed(request,user)
        return JSONResponse({'conversations':[{**one,'id':str(one['id'])} for one in self.chats.listing(user.name,'tools',user.info.get('max_sensitivity','normal'))]})

    async def one(self, request, user):
        """Read an accessible tool conversation under current account permissions."""
        talk=self.talk(request,user)
        await self.allowed(request,user,talk['id'],talk['level'])
        return JSONResponse(self.shown(talk))

    async def body(self, request, fields):
        """Bound JSON arguments and reject unknown operation fields."""
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>1024*1024:
                raise Problem('Tool chat requests must fit within 1 MiB.',413)
        try:
            body=json.loads(raw)
            if not isinstance(body,dict) or body.keys()-set(fields):raise ValueError()
        except (ValueError,TypeError):
            raise Problem('Expected a JSON object with fields: '+', '.join(fields), 400, 'unsupported_parameter') from None
        return body

    async def start(self, request, user):
        """Create an empty private tool conversation before any explicit call or message."""
        await self.allowed(request,user)
        number=self.chats.start(user.name,'Tool conversation',kind='tools')
        return JSONResponse({'id':str(number)},201)

    async def change(self, request, user):
        """Reuse the owner-private history rename and removal semantics."""
        talk=self.talk(request,user);await self.allowed(request,user,talk['id'],talk['level'])
        body=await self.body(request,('title','delete'))
        if 'delete' in body and type(body['delete']) is not bool:raise Problem('delete must be true or false.')
        if body.get('delete'):
            if str(talk['id']) in self.turns:raise Problem('Stop this conversation before deleting it.',409)
            self.chats.remove(talk['id']);return JSONResponse({'removed':str(talk['id'])})
        title=body.get('title')
        if not isinstance(title,str) or not title.strip() or len(title)>120:raise Problem('Conversation names contain 1–120 characters.')
        self.chats.change(talk['id'],title=title.strip());return JSONResponse({'title':title.strip()})

    def limits(self, client, state):
        """Retain call counts and wall time across approval waits and process restarts."""
        settings=self.settings()
        return ToolTurn(client,state.get('model',''),hosted=state.get('hosted',False),
            max_calls=settings.get('response_max_tool_calls',25),max_seconds=settings.get('response_max_tool_seconds',900),
            calls=state.get('calls',0),started=time.monotonic()-max(0,time.time()-state['started']))

    def messages(self, talk):
        """Reconstruct model history with bounded tool copies and exact originals kept separately."""
        result=[]
        for message in talk['messages']:
            if message.get('model_message'):result.append(json.loads(json.dumps(message['model_message'])))
            if message.get('executed_call'):
                call=message['executed_call']
                for previous in reversed(result):
                    original=next((one for one in previous.get('tool_calls',[]) if one.get('id')==call['id']),None)
                    if original is not None:
                        original['function']=call['function']
                        break
            if not message.get('model_message') and message['role']=='user':result.append({'role':'user','content':message['content']})
        return result

    def begin(self, request, user, talk, client, state, *, decision=None):
        """Register one cancellable task in the existing chat lifecycle."""
        ref=str(talk['id'])
        turn=self.turn_type(model=state.get('model',''),owner=user.name,state='Working…')
        self.turns[ref]=[turn];self.chats.answering(talk['id'],0,user.name,turn.model)
        turn.task=asyncio.create_task(self.run(user,talk,client,state,turn,decision))
        def finished(task):
            """Release ownership even when cancellation arrives before the coroutine starts."""
            if not turn.done:
                if self.chats.get(talk['id'], user.name):
                    self.close_calls(talk['id'],user,client,'Interrupted; not replayed.')
                    self.chats.say(talk['id'],'assistant','',error='Stopped before execution.',level=client.current_level())
                    self.chats.tool_state(talk['id'],{},client.current_level())
                self.chats.answered(talk['id'],0)
                turn.done=True;self.turns.pop(ref,None)
            if not task.cancelled():task.exception()
        turn.task.add_done_callback(finished)

    async def act(self, request, user):
        """Accept a direct call, model message, explicit decision, or existing-task cancellation."""
        talk=self.talk(request,user);number=talk['id'];ref=str(number)
        body=await self.body(request,('do','name','arguments','content','model','call_id','approve','reason','toolset'))
        action=body.get('do','say')
        if action=='stop':
            group=self.turns.get(ref)
            if not group:raise Problem('No request is running.',409)
            for turn in group:
                turn.stopped=True
                if turn.task:turn.task.cancel()
            return JSONResponse({'stopped':ref})
        client=await self.allowed(request,user,number,talk['level'],body.get('toolset',talk['tool_state'].get('toolset','')))
        talk=self.talk(request,user)
        client.level=max(client.level,talk['level'],key=LEVELS.index)
        if ref in self.turns:raise Problem('Wait for this request, or stop it.',409)
        if action=='decision':
            state=talk['tool_state'];pending=state.get('pending',[])
            call=next((one for one in pending if one.get('id')==body.get('call_id')),None)
            if call is None:raise Problem('This proposal is no longer pending.',409)
            if type(body.get('approve')) is not bool:raise Problem('approve must be true or false.')
            args=body.get('arguments')
            if args is not None and not isinstance(args,dict):raise Problem('arguments must be a JSON object.')
            reason=body.get('reason','')
            if not isinstance(reason,str) or len(reason)>4000:raise Problem('Decline reason must fit within 4,000 characters.')
            decision={'call_id':call['id'],'approve':body['approve'],'reason':reason,'arguments':args}
            # Consume the proposal durably before executing it. Interrupted calls are never replayed.
            state={**state,'pending':[one for one in pending if one['id']!=call['id']]}
            self.chats.tool_state(number,state,talk['level'])
            self.begin(request,user,talk,client,state,decision=(call,decision))
        elif action=='direct':
            if talk['tool_state'].get('pending'):raise Problem('Resolve the pending model proposals first.',409)
            name,args=body.get('name'),body.get('arguments',{})
            if not isinstance(name,str) or name not in {one['name'] for one in client.tools} or not isinstance(args,dict):raise Problem('Choose an available tool and JSON object arguments.')
            state={'started':time.time(),'calls':0,'toolset':body.get('toolset',''),'direct':{'id':'direct-'+str(time.time_ns()),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}}
            self.chats.tool_state(number,state,talk['level']);self.begin(request,user,talk,client,state)
        elif action=='say':
            if talk['tool_state'].get('pending'):raise Problem('Resolve the pending model proposals first.',409)
            model,content=body.get('model'),body.get('content')
            options=user.options(model) if isinstance(model,str) else {}
            if model not in user.info.get('models',[]) or not options.get('tools'):raise Problem('Choose a permitted model that supports tools.',400)
            if not isinstance(content,str) or not content.strip() or len(content)>200000:raise Problem('Enter a message of at most 200,000 characters.')
            self.chats.say(number,'user',content,level=talk['level'])
            if not talk['messages']:self.chats.change(number,title=content.strip().splitlines()[0][:60])
            state={'started':time.time(),'calls':0,'model':model,'hosted':bool(options.get('hosted')),'toolset':body.get('toolset','')}
            self.chats.tool_state(number,state,talk['level']);self.begin(request,user,self.chats.get(number,user.name),client,state)
        else:raise Problem('Choose say, direct, decision or stop.')
        return JSONResponse({'id':ref},202)

    def close_calls(self, number, user, client, reason):
        """Resolve interrupted protocol calls as errors without repeating any execution."""
        history=self.messages(self.chats.get(number,user.name))
        pending={}
        for message in history:
            if message.get('role')=='tool':pending.pop(message.get('tool_call_id'),None)
            for call in message.get('tool_calls',[]):pending[call['id']]=call
        for call in pending.values():
            result={'content':[{'type':'text','text':reason}],'isError':True}
            self.chats.say(number,'tool',exact_result(result),level=client.current_level(),tool_call=call,exact=result,
                decision={'interrupted':True},model_message={'role':'tool','tool_call_id':call['id'],'content':model_result(result)})

    async def run(self, user, talk, client, state, turn, decision):
        """Run shared loop steps until a model answer, explicit approval wait, failure or stop."""
        number=talk['id'];ref=str(number);loop=self.limits(client,state)
        try:
            if decision or state.get('direct'):
                original,choice=decision if decision else (state['direct'],{'approve':True,'direct':True})
                call=json.loads(json.dumps(original))
                if choice.get('arguments') is not None:call['function']['arguments']=json.dumps(choice['arguments'])
                if not decision:
                    self.chats.say(number,'assistant','',level=client.current_level(),tool_call=call,decision=choice,
                        model_message={'role':'assistant','content':None,'tool_calls':[call]})
                loop.remaining()
                if choice['approve']:
                    result=await loop.execute(call,count=not decision)
                else:result={'content':[{'type':'text','text':'Tool call declined.'+(' '+choice['reason'] if choice.get('reason') else '')}],'isError':True}
                level=client.current_level()
                self.chats.tool_state(number,state,level)
                self.chats.say(number,'tool',exact_result(result),level=level,tool_call=original,executed_call=call,decision=choice,exact=result,
                               model_message={'role':'tool','tool_call_id':call['id'],'content':model_result(result)})
                if state.get('direct') or state.get('pending'):
                    return
            messages=self.messages(self.chats.get(number,user.name))
            async def model(history, definitions):
                """Use ordinary model admission and preserve one conversation trace and raised level."""
                # Resolve current account policy again after every approval wait and tool result.
                me=await self.compute.get('/loadcoach/me',headers={'authorization':client.authorization})
                if not me.is_success or 'use-tools' not in me.json().get('can',[]):raise Problem('Your account may no longer use tools.',403)
                options=me.json().get('chat',{}).get('models',{}).get(state['model'],{})
                if not options.get('tools'):raise Problem('This model is no longer available for tool calls.',403)
                if options.get('hosted') and client.current_level() != 'normal':
                    raise Problem(f"A tool result is marked {client.current_level()} and may not be sent to the hosted model {state['model']}.",403,'tool_result_too_sensitive')
                response=await self.inference.post('/v1/chat/completions',json={'model':state['model'],'messages':history,'tools':definitions,'stream':False},
                    headers={'authorization':client.authorization,'x-loadcoach-trace':client.trace,'x-loadcoach-sensitivity':client.current_level()},timeout=None)
                if not response.is_success:
                    try:message=response.json()['error']['message']
                    except (ValueError,KeyError,TypeError):message='The model request failed.'
                    raise Problem(message,response.status_code)
                return response.json()
            async def approve(call):
                """Every model-proposed call waits for the person's explicit decision."""
                return None
            pending=[]
            async for step in loop.run(messages,client.definitions(),model,approve=approve):
                if step['type']=='model':
                    message=step['result']['choices'][0]['message']
                    self.chats.say(number,'assistant',message.get('content') or '',level=client.current_level(),model=state['model'],model_message=message)
                elif step['type']=='approval':pending.append(step['call'])
            self.chats.tool_state(number,{**state,'calls':loop.calls,'pending':pending},client.current_level())
        except ToolLimit as error:
            self.close_calls(number,user,client,str(error))
            self.chats.say(number,'assistant','',error=error.reason,level=client.current_level())
            self.chats.tool_state(number,{},client.current_level())
        except asyncio.CancelledError:
            self.close_calls(number,user,client,'Interrupted; not replayed.')
            self.chats.say(number,'assistant','',error='Stopped. An interrupted tool call is never replayed automatically.',level=client.current_level())
            self.chats.tool_state(number,{},client.current_level())
        except (Problem,httpx.HTTPError,ValueError,KeyError,TypeError,IndexError) as error:
            self.close_calls(number,user,client,str(error))
            self.chats.say(number,'assistant','',error=str(error),level=client.current_level())
            self.chats.tool_state(number,{},client.current_level())
        finally:
            self.chats.tool_state(number,{**self.chats.get(number,user.name)['tool_state'],'calls':loop.calls},client.current_level())
            self.chats.answered(number,0);turn.done=True;self.turns.pop(ref,None)

    def routes(self, route):
        """Register inside the existing authenticated conversation application."""
        def guard(handler):
            """Translate expected contract failures without exposing server internals."""
            async def checked(request,user):
                """Run one caller-scoped operation and return a private response."""
                try:
                    if request.method == 'POST' and request.headers.get('x-loadcoach-log','').lower() == 'off':
                        raise Problem('Tool chat requires logging; omit X-LoadCoach-Log: off.')
                    response = await handler(request,user)
                    response.headers.update(PRIVATE)
                    return response
                except Problem as error:return failure(error.status,str(error),error.code)
            return checked
        return [route('/loadcoach/tool-conversations/tools',guard(self.catalogue)),
            route('/loadcoach/tool-conversations',guard(self.listing)),route('/loadcoach/tool-conversations',guard(self.start),'POST'),
            route('/loadcoach/tool-conversations/{ref}',guard(self.one)),route('/loadcoach/tool-conversations/{ref}',guard(self.change),'POST'),
            route('/loadcoach/tool-conversations/{ref}/actions',guard(self.act),'POST')]
