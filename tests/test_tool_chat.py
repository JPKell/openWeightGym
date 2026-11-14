"""Private Tool chat: approvals, caller identity, exact history, limits and cancellation."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from weightroom.conversations import build_app
from weightroom.chat_store import Conversations


@pytest.fixture
def tool_chat(tmp_path):
    rig=SimpleNamespace(executions=[],models=[],level='normal',removed=False,unavailable=False,block=False,closed=False,
        repeat=False,no_tools=False,limit=25,seconds=900,result={'content':[{'type':'text','text':'exact <result>'},{'type':'image','data':'YWJj','mimeType':'image/png'}],'isError':False,'structuredContent':{'zero':0}},
        infos={name:{'name':name,'identity':name+'-id','can':['use-tools'],'max_sensitivity':'restricted','models':['local','cloud'],
                     'chat':{'models':{'local':{'tools':True},'cloud':{'tools':True,'hosted':True}}}} for name in ('alice','bob')})
    schema={'type':'object','properties':{'text':{'type':'string','description':'Text to echo'},'count':{'type':'integer','enum':[0,1]}},'required':['text']}
    async def wire(request):
        who=request.headers.get('authorization','').removeprefix('Bearer ')
        if request.url.path=='/loadcoach/me':return httpx.Response(200,json=rig.infos[who])
        if request.method=='GET':
            return httpx.Response(200,json={'busy':0,'running':[],'waiting':[],'holds':[],'loaded':[],'cards':[],'memory':{},'models':[],'can':[],'plugins':[],'programs':[]})
        body=json.loads(request.content or b'{}')
        if request.url.path.startswith('/mcp'):
            if rig.unavailable:return httpx.Response(503,json={'error':{}})
            method=body['method'];names=[] if rig.no_tools else ['fake__echo','fake__secret'] if who=='alice' else ['other__echo']
            if method=='initialize':result={'protocolVersion':'2025-06-18'}
            elif method=='tools/list':result={'tools':[{'name':name,'description':'A test tool','inputSchema':schema} for name in names]}
            else:
                name=body['params']['name']
                if name not in names or rig.removed:return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'error':{'message':'Unknown tool'}})
                rig.executions.append((who,body['params'],dict(request.headers)))
                if rig.block:
                    try:await asyncio.Event().wait()
                    finally:rig.closed=True
                if name=='fake__secret':rig.level='sensitive'
                result=rig.result
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result},headers={'mcp-session-id':'session-'+who})
        rig.models.append(body)
        if not body['messages'] or body['messages'][-1]['role']!='tool' or rig.repeat:
            message={'role':'assistant','content':None,'tool_calls':[{'id':getattr(rig,'call_id','call-'+str(len(rig.models))),'type':'function','function':{'name':'fake__echo','arguments':'{"text":"proposed"}'}}]}
            finish='tool_calls'
        else:message={'role':'assistant','content':'Finished'};finish='stop'
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':finish}]})
    http=httpx.AsyncClient(transport=httpx.MockTransport(wire),base_url='http://front')
    rig.http=http
    app=build_app(str(tmp_path/'chat.db'),http,tools=http,tool_level=lambda owner,trace:rig.level,
                  tool_settings=lambda:{'response_max_tool_calls':rig.limit,'response_max_tool_seconds':rig.seconds})
    with TestClient(app,headers={'authorization':'Bearer alice'}) as client:
        yield client,app,rig
        client.portal.call(http.aclose)


def wait(client,number):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        answer=client.get('/loadcoach/tool-conversations/'+number);assert answer.is_success,answer.text
        talk=answer.json()
        if not talk['pending']:return talk
        time.sleep(.01)
    raise AssertionError(talk)


def start(client):
    response=client.post('/loadcoach/tool-conversations',json={});assert response.status_code==201,response.text
    return response.json()['id']


def act(client,number,**body):
    result=client.post('/loadcoach/tool-conversations/'+number+'/actions',json=body)
    assert result.status_code==(200 if body.get('do')=='stop' else 202),result.text
    return wait(client,number)


def test_direct_exact_error_output_identity_and_separate_history(tool_chat):
    client,app,rig=tool_chat;rig.result['isError']=True
    number=start(client)
    talk=act(client,number,do='direct',name='fake__echo',arguments={'text':'<safe>','count':0})
    output=next(message for message in talk['messages'] if message.get('exact'))
    assert output['exact']==rig.result and json.loads(output['content'])==rig.result
    assert rig.executions[0][0]=='alice' and rig.executions[0][1]['arguments']['count']==0
    assert rig.executions[0][2]['x-loadcoach-trace']=='chat-'+number
    assert client.get('/loadcoach/conversations').json()['conversations']==[]
    assert client.get('/loadcoach/conversations/'+number).status_code==404
    assert client.get('/loadcoach/tool-conversations/'+number,headers={'authorization':'Bearer bob'}).status_code==404
    assert client.post('/loadcoach/tool-conversations/'+number,json={'title':'Renamed'}).json()['title']=='Renamed'
    assert client.post('/loadcoach/tool-conversations/'+number,json={'delete':True}).is_success
    assert client.get('/loadcoach/tool-conversations/'+number).status_code==404


@pytest.mark.parametrize('approve',[True,False])
def test_model_proposal_edit_run_continue_or_decline(tool_chat,approve):
    client,app,rig=tool_chat;number=start(client)
    talk=act(client,number,do='say',model='local',content='Use the tool')
    proposal=talk['tool_state']['pending'][0];assert rig.executions==[]
    talk=act(client,number,do='decision',call_id=proposal['id'],approve=approve,arguments={'text':'edited'},reason='No thanks')
    assert talk['messages'][-1]['content']=='Finished'
    result=next(message for message in talk['messages'] if message.get('exact'))
    assert result['decision']['approve'] is approve
    if approve:
        assert rig.executions[0][1]['arguments']=={'text':'edited'}
        assert rig.models[-1]['messages'][-2]['tool_calls'][0]['function']['arguments']=='{"text": "edited"}'
    else:
        assert not rig.executions and 'No thanks' in rig.models[-1]['messages'][-1]['content']
    repeated=client.post('/loadcoach/tool-conversations/'+number+'/actions',json={'do':'decision','call_id':proposal['id'],'approve':True})
    assert repeated.status_code==409


def test_caller_catalogues_ability_and_midrun_removal(tool_chat):
    client,app,rig=tool_chat
    alice=client.get('/loadcoach/tool-conversations/tools').json()['tools']
    bob=client.get('/loadcoach/tool-conversations/tools',headers={'authorization':'Bearer bob'}).json()['tools']
    assert {one['name'] for one in alice}=={'fake__echo','fake__secret'}
    assert {one['name'] for one in bob}=={'other__echo'}
    number=start(client);talk=act(client,number,do='say',model='local',content='Use a tool')
    rig.removed=True
    talk=act(client,number,do='decision',call_id=talk['tool_state']['pending'][0]['id'],approve=True)
    assert not rig.executions and 'Unknown tool' in rig.models[-1]['messages'][-1]['content']
    rig.infos['alice']['can']=[]
    assert client.get('/loadcoach/tool-conversations').status_code==403


def test_sensitive_tool_stops_cloud_and_later_clearance_hides_history(tool_chat):
    client,app,rig=tool_chat;number=start(client)
    act(client,number,do='direct',name='fake__secret',arguments={'text':'secret'})
    talk=act(client,number,do='say',model='cloud',content='Read the result')
    assert not rig.models and 'tool result is marked sensitive' in talk['messages'][-1]['error']
    assert talk['level']=='sensitive'
    rig.infos['alice']['max_sensitivity']='normal'
    assert client.get('/loadcoach/tool-conversations/'+number).status_code==404
    assert client.get('/loadcoach/tool-conversations').json()['conversations']==[]


@pytest.mark.parametrize('kind',['calls','time'])
def test_limits_include_approval_waits(tool_chat,kind):
    client,app,rig=tool_chat;number=start(client);rig.repeat=True
    if kind=='calls':rig.limit=1
    talk=act(client,number,do='say',model='local',content='Use tools')
    if kind=='time':
        state=talk['tool_state'];state['started']-=1000;app.state.chats.tool_state(int(number),state,'normal')
    talk=act(client,number,do='decision',call_id=talk['tool_state']['pending'][0]['id'],approve=True)
    assert any(message.get('error')==('max_tool_calls' if kind=='calls' else 'max_tool_time') for message in talk['messages'])
    if kind=='time':assert not rig.executions


def test_stopping_uses_existing_cancellable_chat_tasks(tool_chat):
    client,app,rig=tool_chat;number=start(client);rig.block=True
    response=client.post('/loadcoach/tool-conversations/'+number+'/actions',json={'do':'direct','name':'fake__echo','arguments':{'text':'wait'}})
    assert response.status_code==202
    deadline=time.monotonic()+2
    while not rig.executions and time.monotonic()<deadline:time.sleep(.01)
    rig.unavailable=True
    result=client.post('/loadcoach/tool-conversations/'+number+'/actions',json={'do':'stop'})
    assert result.status_code==200
    rig.unavailable=False
    talk=wait(client,number)
    assert rig.closed and 'Stopped' in talk['messages'][-1]['error'] and not talk['tool_state'].get('pending')
    assert any(message.get('tool_call',{}).get('function',{}).get('arguments') == '{"text": "wait"}' for message in talk['messages'])
    assert any(message.get('exact',{}).get('isError') for message in talk['messages'])


def test_no_visible_tools_and_unavailability_have_explicit_errors(tool_chat):
    client,app,rig=tool_chat
    rig.no_tools=True
    assert client.get('/loadcoach/tool-conversations/tools').status_code==403
    rig.no_tools=False;rig.unavailable=True
    result=client.get('/loadcoach/tool-conversations/tools')
    assert result.status_code==503 and 'unavailable' in result.text


def test_approval_state_is_not_replayed_after_restart(tool_chat):
    client,app,rig=tool_chat;number=start(client)
    talk=act(client,number,do='say',model='local',content='Use a tool')
    store=app.state.chats
    # A proposal survives an idle restart; an active execution is recovered as an error.
    assert talk['tool_state']['pending'] and not rig.executions
    store.answering(int(number),0,'alice-id','local')
    restarted=build_app(store.db.database,rig.http,tools=rig.http)
    with TestClient(restarted,headers={'authorization':'Bearer alice'}) as second:
        recovered=second.get('/loadcoach/tool-conversations/'+number).json()
    assert not rig.executions and not recovered['tool_state'].get('pending')
    assert any(message.get('decision',{}).get('interrupted') for message in recovered['messages'])


@pytest.mark.parametrize('body',[{'do':'direct','name':[]},{'do':'say','model':{},'content':'hi'},{'do':'direct','toolset':1}])
def test_malformed_tool_actions_are_rejected_before_execution(tool_chat,body):
    client,app,rig=tool_chat;number=start(client)
    answer=client.post('/loadcoach/tool-conversations/'+number+'/actions',json=body)
    assert answer.status_code==400,answer.text
    assert not rig.executions and not rig.models


def test_log_off_cannot_create_tool_history(tool_chat):
    client,app,rig=tool_chat
    answer=client.post('/loadcoach/tool-conversations',json={},headers={'x-loadcoach-log':'off'})
    assert answer.status_code==400 and app.state.chats.listing('alice-id','tools')==[]
    assert answer.headers['cache-control']=='private, no-store' and answer.headers['x-content-type-options']=='nosniff'



def test_reused_native_call_ids_do_not_rewrite_earlier_edited_arguments(tool_chat):
    client,app,rig=tool_chat;number=start(client);rig.call_id='reused'
    for text in ('first','second'):
        talk=act(client,number,do='say',model='local',content='Use the tool')
        act(client,number,do='decision',call_id=talk['tool_state']['pending'][0]['id'],approve=True,arguments={'text':text})
    proposals=[one for one in rig.models[-1]['messages'] if one.get('tool_calls')]
    assert [json.loads(one['tool_calls'][0]['function']['arguments']) for one in proposals]==[{'text':'first'},{'text':'second'}]


def test_only_model_facing_copy_of_large_tool_result_is_bounded(tool_chat):
    client,app,rig=tool_chat;number=start(client)
    rig.result['content'][0]['text']='x'*100000
    talk=act(client,number,do='say',model='local',content='Use a tool')
    talk=act(client,number,do='decision',call_id=talk['tool_state']['pending'][0]['id'],approve=True)
    exact=next(one['exact'] for one in talk['messages'] if one.get('exact'))
    assert exact==rig.result and len(exact['content'][0]['text'])==100000
    sent=rig.models[-1]['messages'][-1]['content']
    assert len(sent)<66000 and 'truncated' in sent
