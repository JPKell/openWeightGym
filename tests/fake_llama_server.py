"""Stands in for llama-server in tests: same flags, same /health, no model.

It answers every POST with a description of itself, so a test can see which process answered and
what it was started with. `--fail` makes it exit at once, like a model that will not load, and
`--bad-flag` like a flag llama-server does not know, each printing what the real one prints. A
request body with `"sleep": seconds` takes that long to answer, like a slow generation, and one
with `"reply": {...}` is answered with exactly that, so a test can play the model. `"drip": seconds`
streams one small piece of thinking after another for that long, like a model caught in a loop.

`--mib N` makes it use N MiB of each card it was given, as the stand-in for nvidia-smi then
reports, and exit as the real one does if a card has not that much left. A request body with
`"grow": N` makes it use N more from then on, like a model that grows while it answers.
`--takes N` makes it need N seconds to load. `--context-mib` adds the context size to
`--mib`, so context-fitting tests observe a different allocation at each size.
"""

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def coverage_answer(messages):
    """Scripted Part 8 responder: only visible questions and actual function results."""
    question=messages[1]['content']
    functions={call['id']:call['function'] for message in messages for call in message.get('tool_calls',[])}
    replies=[]
    for message in messages:
        if message['role']=='tool':
            function=functions[message['tool_call_id']]
            replies.append((function['name'],json.loads(function['arguments']),json.loads(message['content'])))
    done=lambda name:next((value for tool,args,value in replies if tool==name),None)
    if 'supplied token' in question:
        return None,{'echo':re.search(r'TOKEN-\d+',question)[0]}
    if 'calendar event' in question:
        return None,{'status':'unsupported'}
    if 'weather' in question or 'forecast' in question:
        name='forecast' if "tomorrow's forecast" in question else 'weather'
        if done(name) is None:
            return (name,{'city':re.search(r'Cedar \d+',question)[0]}),None
    if 'metres' in question or 'length' in question:
        lengths=[int(n) for n in re.findall(r'(\d+) metres',question)]
        if not lengths:
            lengths=[int(re.search(r'(\d+) centimetres',question)[1])//100]
        for metres in lengths:
            if not any(name=='convert_length' and args['metres']==metres for name,args,value in replies):
                return ('convert_length',{'metres':metres}),None
        values=[next(value['centimetres'] for name,args,value in replies if name=='convert_length' and args['metres']==n) for n in lengths]
        if len(lengths)>1:
            return None,dict(left_cm=values[0],right_cm=values[1])
        result={'centimetres':values[0]}
        if done('weather') is not None:
            result.update(done('weather'))
        return None,result
    if 'cents to USD' in question:
        if done('convert_currency') is None:
            return ('convert_currency',dict(cents=int(re.search(r'(\d+) cents',question)[1]),currency='USD')),None
        return None,done('convert_currency')
    if 'customer' in question:
        if done('find_record') is None:
            customer=re.search(r'customer "([^"]+)"',question)[1]
            if ', ' in customer:
                last,first=customer.split(', ');customer=first+' '+last
            return ('find_record',{'customer':customer}),None
        record=done('find_record')['record_id']
    else:
        found=re.search(r'record id "([^"]+)"',question)
        record=found[1] if found else None
    if record:
        if 'note with text' in question:
            name='create_note'
            params=dict(record_id=record,text=re.search(r'text "([^"]+)"',question)[1],request_id=re.search(r'request_id "([^"]+)"',question)[1])
        else:
            name='public_record' if 'public summary' in question else 'read_record'
            params={'record_id':record}
        if done(name) is None:
            return (name,params),None
        return None,done(name)
    return None,done('forecast') or done('weather')


arguments = sys.argv[1:]
if arguments == ["--help"]:  # a few lines in the shape of the real help: headings, short and long spellings, text that runs on
    print("""----- common params -----

-h,    --help, --usage                  print usage and exit
-t,    --threads N                      number of CPU threads to use during generation (default: -1)
                                        (env: LLAMA_ARG_THREADS)
-fa,   --flash-attn [on|off|auto]       set Flash Attention use ('on', 'off', or 'auto', default: 'auto')
                                        (env: LLAMA_ARG_FLASH_ATTN)
-kvo,  --kv-offload, -nkvo, --no-kv-offload
                                        whether to enable KV cache offloading (default: enabled)
                                        (env: LLAMA_ARG_KV_OFFLOAD)
-sm,   --split-mode {none,layer,row,tensor}
                                        how to split the model across multiple GPUs, one of:
                                        - none: use one GPU only
                                        - layer (default): split layers and KV across GPUs
--host HOST                             ip address to listen (default: 127.0.0.1)

----- sampling params -----

--temp, --temperature N                 temperature (default: 0.80)
""")
    sys.exit(0)
if arguments == ["--version"]:  # as the real one answers
    print("version: 1 (fake)\nbuilt with nothing for tests")
    sys.exit(0)
if arguments == ["--list-devices"]:
    print("Available devices:")
    for index in range(int(os.environ.get("FAKE_DEVICE_COUNT", "2"))):
        print(f"  GPU{index}: Fake GPU (16000 MiB, 16000 MiB free)")
    sys.exit(0)
print("0.00.001.000 I fake: starting with " + " ".join(arguments), flush=True)
if "--fail" in arguments:  # as llama-server reports running out of memory: the cause first, then what follows
    print("0.00.002.000 E ggml_backend_cuda_buffer_type_alloc_buffer: allocating 12000.00 MiB on device 1: cudaMalloc failed: out of memory")
    print("0.00.003.000 E srv  llama_server: exiting due to model loading error", flush=True)
    sys.exit(3)
if "--bad-flag" in arguments:  # as llama-server reports a flag it does not know
    print("error: invalid argument: --bad-flag", file=sys.stderr, flush=True)
    sys.exit(1)


def flag(name):
    return arguments[arguments.index(name) + 1]


def use(mib):
    """Take `mib` MiB of each card this was started on, on top of what it has. See fake_nvidia_smi.py."""
    from fake_nvidia_smi import FOLDER, TOTAL, held

    mine = FOLDER / f"{os.getpid()}.json"
    now = {int(card[-1]): held().get(os.getpid(), {}).get(int(card[-1]), 0) + mib for card in flag("--device").split(",")}
    if any(sum(uses.get(card, 0) for pid, uses in held().items() if pid != os.getpid()) + wanted > TOTAL for card, wanted in now.items()):
        print(f"0.00.002.000 E ggml_backend_cuda_buffer_type_alloc_buffer: allocating {mib}.00 MiB: cudaMalloc failed: out of memory", flush=True)
        sys.exit(3)
    mine.write_text(json.dumps(now))


if "--mib" in arguments:
    needed = int(flag("--mib")) + (int(flag("--ctx-size")) if "--context-mib" in arguments else 0)
    if "--split-total" in arguments:
        needed //= len(flag("--device").split(","))
    use(needed)
if "--takes" in arguments:  # seconds it needs to load, before it answers /health
    time.sleep(float(flag("--takes")))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):  # /health
        self.send_response(200)
        self.end_headers()
        if self.path == '/v1/models':
            self.wfile.write(json.dumps({'data': [{'architecture': {'output_modalities': ['decisions']}}]}).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        request = json.loads(body)
        if self.path == '/v1/systemone':
            answers = {}
            for key, q in request['questions'].items():
                if q['type'] == 'noul':
                    answers[key] = {'type': 'noul', 'noul': .8}
                else:
                    choices = list(q['criteria']) if q['type'] == 'choice' else [str(i) for i in range(len(q['criteria']))]
                    p = {choice: (.8 if i == 0 else .2 / (len(choices) - 1)) for i, choice in enumerate(choices)}
                    answers[key] = {'type': q['type'], 'probabilities': p, 'confidence': .7}
                    answers[key].update({'choice': choices[0]} if q['type'] == 'choice' else
                                        {'score': sum(int(i) * v for i, v in p.items()), 'legend': dict(zip(choices, q['criteria']))})
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'model': request['model'], 'answers': answers,
                                        'usage': {'input_tokens': 99, 'output_tokens': 0}}).encode())
            return
        if ('--benchmark-dialogue' in arguments and self.path in ('/apply-template','/v1/chat/completions')
                and (self.path=='/apply-template' or request.get('temperature') in (0,1) and request.get('seed')==42
                     and request.get('cache_prompt') is False and request.get('stream') is False)):
            # A native transport fixture, not a benchmark answer-key reader.
            prompt = json.dumps({'messages':request['messages'],'tools':request.get('tools',[])},sort_keys=True)
            rejected = ('--benchmark-template-reject' in arguments and self.path=='/apply-template'
                        or '--benchmark-dialogue-reject' in arguments and self.path=='/v1/chat/completions')
            if rejected:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"unsupported template or JSON Schema grammar"}')
                return
            if '--benchmark-dialogue-unparseable' in arguments and self.path=='/v1/chat/completions':
                self.send_response(500)  # as llama-server answers when it cannot parse the model's own tool-call output
                self.end_headers()
                self.wfile.write(b'{"error":{"code":500,"message":"The model produced output that does not match the expected peg-native format","type":"server_error"}}')
                return
            if self.path=='/apply-template':
                result = {'prompt':prompt}
            else:
                assert request['temperature'] in (0,1) and request['seed']==42 and request['cache_prompt'] is False
                if request['temperature']==1:
                    assert {key:request[key] for key in ('top_p','top_k','min_p','repeat_penalty',
                        'presence_penalty','frequency_penalty')}=={
                        'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
                        'presence_penalty':0.0,'frequency_penalty':0.0}
                assert request['stream'] is False
                if not request.get('tools'):
                    assert 'tool_choice' not in request and 'parallel_tool_calls' not in request
                message = {'role':'assistant','content':'{"code":"amber"}','reasoning_content':'private reasoning'}
                if request.get('tools'):
                    message.update(content=None,tool_calls=[{'id':'native-call','type':'function','function':{'name':request['tools'][0]['function']['name'],'arguments':'{}'}}])
                result = {'choices':[{'message':message,'finish_reason':'stop'}],
                          'usage':{'prompt_tokens':len(prompt),'completion_tokens':16},
                          'timings':{'cache_n':0,'prompt_ms':4,'predicted_ms':8}}
                if '--benchmark-reasoning-count' in arguments:
                    result['usage']['completion_tokens_details']={'reasoning_tokens':json.loads(flag('--benchmark-reasoning-count'))}
                if '--benchmark-delay' in arguments:
                    time.sleep(float(flag('--benchmark-delay')))
                if '--benchmark-dialogue-malformed' in arguments:
                    result['usage']['prompt_tokens'] -= 1
            self.send_response(200)
            self.send_header('content-type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        if '--benchmark-tools' in arguments and request.get('tools') and self.path in ('/apply-template', '/v1/chat/completions'):
            # A scripted native answerer reads only the question and actual tool responses.
            # It never imports a benchmark fixture or receives its answer key.
            prompt = json.dumps({'messages': request['messages'], 'tools': request.get('tools', [])}, sort_keys=True)
            if self.path == '/apply-template':
                result = {'prompt': prompt}
            else:
                expanded = request['messages'][0]['content'].startswith('Function use is optional')
                assert request['temperature'] == (1 if expanded else 0)
                assert request['seed'] == 42 and request['cache_prompt'] is False
                if expanded:
                    assert {name:request[name] for name in ('top_p','top_k','min_p','repeat_penalty',
                             'presence_penalty','frequency_penalty')} == {
                        'top_p':1.0,'top_k':0,'min_p':0.0,'repeat_penalty':1.0,
                        'presence_penalty':0.0,'frequency_penalty':0.0}
                assert request['parallel_tool_calls'] is False and request['tool_choice'] == 'auto'
                question = request['messages'][1]['content']
                last = request['messages'][-1]
                action = None
                if expanded:
                    action, reply = coverage_answer(request['messages'])
                else:
                    if last['role'] == 'user':
                        if 'current weather' in question:
                            action = ('weather', {'city': re.search(r'for (Cedar \d+)', question)[1]})
                        elif 'convert' in question:
                            action = ('convert_length', {'metres': int(re.search(r'convert (\d+) metres', question)[1])})
                        else:
                            action = ('find_record', {'customer': question.split('Find ', 1)[1].split("'s record", 1)[0]})
                    else:
                        reply = json.loads(last['content'])
                        if reply.get('retryable'):
                            previous = request['messages'][-2]['tool_calls'][0]['function']
                            action = (previous['name'], json.loads(previous['arguments']))
                        elif reply.get('error') == 'record_moved':
                            action = ('read_record', {'record_id': reply['record_id']})
                        elif reply.get('error') == 'permission_denied':
                            previous = request['messages'][-2]['tool_calls'][0]['function']
                            action = ('public_record', json.loads(previous['arguments']))
                        elif 'record_id' in reply:
                            if 'create exactly one note' in question:
                                text, request_id = re.findall(r'"([^"]+)"', question)
                                action = ('create_note', {'record_id': reply['record_id'], 'text': text, 'request_id': request_id})
                            else:
                                action = ('read_record', {'record_id': reply['record_id']})
                message = {'role': 'assistant', 'content': None if action else json.dumps(reply)}
                if action:
                    message['tool_calls'] = [{'id': 'call-' + str(len(request['messages'])), 'type': 'function',
                                              'function': {'name': action[0], 'arguments': json.dumps(action[1])}}]
                if '--benchmark-delay' in arguments:
                    time.sleep(float(flag('--benchmark-delay')))
                result = {'choices': [{'message': message, 'finish_reason': 'tool_calls' if action else 'stop'}],
                          'usage': {'prompt_tokens': len(prompt), 'completion_tokens': 16}, 'timings': {'cache_n': 0}}
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        if '--benchmark-workflow' in arguments and self.path=='/v1/chat/completions' and request.get('stream') is False:
            supplied=json.loads(request['messages'][-1]['content'])
            source=supplied['sources'][0]
            answer=('Draft: '+supplied['accepted_research'] if 'accepted_research' in supplied else source['text'].splitlines()[0])
            content=json.dumps({'answer':answer,'source_ids':[source['id']],'abstain':False})
            result={'choices':[{'message':{'role':'assistant','content':content},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':len(json.dumps(request['messages'])),'completion_tokens':16}}
            self.send_response(200)
            self.send_header('content-type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        if self.path in ('/apply-template','/detokenize'):
            result = ({'prompt':'<user>'+request['messages'][0]['content']+'</user><assistant>'}
                      if self.path=='/apply-template' else {'content':''.join(chr(t) for t in request['tokens'])})
            self.send_response(200)
            self.send_header('content-type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        if self.path == '/tokenize':
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'tokens': [ord(c) for c in request['content']] if 'parse_special' in request else list(range(1, 33))}).encode())
            return
        if self.path == '/v1/embeddings':
            assert '--embedding' in arguments
            assert request['encoding_format'] == 'float' and request.get('cache_prompt', False) is False
            tokens = request['input']
            assert isinstance(tokens,str) and tokens or isinstance(tokens,list) and tokens and all(type(token) is int for token in tokens)
            limit = int(flag('--benchmark-embedding-limit')) if '--benchmark-embedding-limit' in arguments else int(flag('--ctx-size'))
            if len(tokens) > limit or ('--benchmark-embedding-exclusive-context' in arguments
                                       and len(tokens) >= int(flag('--ctx-size'))):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({'error':{'message':f'input ({len(tokens)} tokens) is too large to process. increase the physical batch size (current batch size: {limit})'}}).encode())
                return
            print(f'fake embedding input={len(tokens)} cache=false', flush=True)
            time.sleep(float(flag('--benchmark-delay')) if '--benchmark-delay' in arguments else .01)
            self.send_response(200)
            self.send_header('content-type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'data':[{'index':0,'embedding':[.25,.5,.75]}],
                                        'usage':{'prompt_tokens':len(tokens),'total_tokens':len(tokens)}}).encode())
            return
        if self.path == '/completion':
            if '--benchmark-crash-above' in arguments and int(flag('--ctx-size')) > int(flag('--benchmark-crash-above')):
                message = flag('--benchmark-crash-message') if '--benchmark-crash-message' in arguments else 'CUDA error: the resource allocation failed'
                print(message, flush=True)
                os._exit(1)  # a native abort closes HTTP after readiness, before any valid sample
            # Native benchmark protocol: exact token input and final, uncached timing counts.
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.end_headers()
            try:
                output = flag('--benchmark-reply') if '--benchmark-reply' in arguments else 'Measured answer.'
                if not request.get('ignore_eos',True):
                    assert request['temperature'] in (0,1) and request['cache_prompt'] is False
                self.wfile.write(b'data: ' + json.dumps({'content':output,'tokens':[42],'stop':False}).encode()+b'\n\n')
                self.wfile.flush()
                if '--benchmark-delay' in arguments:
                    time.sleep(float(flag('--benchmark-delay')))
                timing = {'prompt_n': len(request['prompt']), 'predicted_n': request['n_predict'] if request.get('ignore_eos',True) else len(output),
                          'prompt_per_second': 1000, 'predicted_per_second': 50, 'cache_n': 0}
                result = {'content': '', 'stop': True, 'stop_type':'limit' if request.get('ignore_eos',True) else 'eos', 'timings': timing}
                self.wfile.write(b'data: ' + json.dumps(result).encode() + b'\n\n')
            except OSError:
                pass
            return
        time.sleep(request.get("sleep", 0))
        if "grow" in request:
            use(request["grow"])
        if "messages" in request and "--ctx-size" in arguments:
            words = sum(len(str(message.get("content", "")).split()) for message in request["messages"])
            if words > int(flag("--ctx-size")):  # here a word is a token
                refusal = {"code": 400, "message": "the request exceeds the available context size, try increasing it",
                           "type": "exceed_context_size_error", "n_prompt_tokens": words, "n_ctx": int(flag("--ctx-size"))}  # fmt: skip
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": refusal}).encode())
                return
        if "drip" in request:
            # Sent in chunks, as llama-server streams. Then a caller can tell an answer that ended from one that was cut off.
            self.protocol_version = "HTTP/1.1"
        self.send_response(request.get("status", 200))  # the test may say that the model server refuses
        if "drip" in request:
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.send_header("connection", "close")
            self.end_headers()
            until, piece = time.monotonic() + request["drip"], 0
            try:
                while time.monotonic() < until:
                    piece += 1
                    delta = {"reasoning_content": f"round {piece}. "}
                    line = b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode() + b"\n\n"
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    time.sleep(0.02)
                self.wfile.write(b"0\r\n\r\n")  # the end, said
            except OSError:
                print("0.00.009.000 I fake: the caller went away", flush=True)
            return
        if "reply" in request:  # the test says what the model answers
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(request["reply"]).encode())
            return
        if request.get("stream") and "messages" in request:
            # A conversation, answered as llama-server streams one. What it says depends on what was last said to it,
            # so that a test can make it think, take its time, or stop short.
            said = request["messages"][-1]["content"]
            if isinstance(said, list):
                said = " ".join(p.get("text", "[image]") for p in said)
            words = sum(len(str(message["content"]).split()) for message in request["messages"])
            thinks = (request.get("chat_template_kwargs") or {}).get("enable_thinking", True)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def send(delta, finish=None, **more):
                self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}], **more}).encode() + b"\n\n")
                self.wfile.flush()

            try:
                if thinks and "think" in said:
                    send({"reasoning_content": "Let me think. "})
                    send({"reasoning_content": "Done thinking."})
                answer = f"You said: {said}" if request["messages"][-1]["role"] == "user" else " and so on."
                if "--answer-prefix" in arguments:
                    answer = flag("--answer-prefix") + " " + answer
                if str(request["messages"][0]["content"]).startswith("Summarize the older conversation"):
                    answer = "Earlier facts include cedar." if "cedar" in said else "Earlier messages summarized."
                    if "FAIL SUMMARY" in said:
                        answer = ""
                for word in answer.split(" "):
                    time.sleep(0.15 if "slowly" in said else 0)
                    send({"content": word + " "})
                send({}, "length" if "cut short" in said else "stop", usage={"prompt_tokens": words, "completion_tokens": len(answer.split())})
                self.wfile.write(b"data: [DONE]\n\n")
            except OSError:
                print("0.00.009.000 I fake: the caller went away", flush=True)
            return
        if request.get("stream"):
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: one\n\ndata: two\n\n")
            return
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "arguments": arguments,
                    "path": self.path,
                    "body": body.decode(),
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "around": {name: os.environ.get(name) for name in ("CUDA_DEVICE_ORDER", "GGML_CUDA_DISABLE_GRAPHS")},  # what LoadCoach set for it
                }
            ).encode()
        )


ThreadingHTTPServer((flag("--host"), int(flag("--port"))), Handler).serve_forever()
