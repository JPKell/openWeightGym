#!/usr/bin/env python3
"""Compare optional observation overhead at matched offered load using isolated HTTP fixtures.

No production service, model, tool or credential is used. Real CPU-model and live
deployment measurements are recorded separately in the final qualification receipt.
"""

import argparse
import asyncio
import gc
import hashlib
import json
import resource
import statistics
import tempfile
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from loadcoach import observability as observation
from loadcoach.groups import Group
from loadcoach.log import Log
from loadcoach.users import User

USER = User('fixture', 'fixture', Group(), identity='fixture-owner')


def fixture(folder, enabled):
    """Build content-free API, CPU, streaming, controlled-tool and rejection cases."""
    async def endpoint(request):
        """Exercise ordinary response paths without external effects or model code."""
        observation.identify(USER)
        kind = request.path_params['kind']
        if kind == 'cpu':
            await asyncio.to_thread(hashlib.pbkdf2_hmac, 'sha256', b'fixture', b'salt', 1000)
        elif kind == 'tool':
            with observation.span('tool_execution', tool_executed=True):
                await asyncio.sleep(.001)
        elif kind == 'stream':
            async def chunks():
                """Yield actual chunks so instrumentation cannot hide body buffering."""
                yield b'data: first\n\n'
                await asyncio.sleep(.001)
                yield b'data: [DONE]\n\n'
            return StreamingResponse(chunks(), media_type='text/event-stream')
        elif kind == 'reject':
            return Response(status_code=401)
        return JSONResponse({'ok':True})
    app = Starlette(routes=[Route('/{kind}', endpoint)])
    log = Log(str(folder/'log.db'))
    sink = observation.instrument(app, 'fixture', log) if enabled else None
    return app, sink, log


async def measure(folder, kind, concurrency, enabled, samples, rate):
    """Measure client-observed completion at a fixed offered rate, including queueing."""
    app, sink, log = fixture(folder, enabled)
    semaphore = asyncio.Semaphore(concurrency)
    times = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        for _ in range(12):
            reply = await client.get('/'+kind)
            assert reply.status_code == (401 if kind=='reject' else 200)
        began = time.perf_counter()
        async def one(index):
            """Keep the offered load identical while bounding concurrent fixture calls."""
            scheduled = began+index/rate
            await asyncio.sleep(max(0, scheduled-time.perf_counter()))
            async with semaphore:
                reply = await client.get('/'+kind)
                assert reply.status_code == (401 if kind=='reject' else 200)
                if enabled:
                    assert observation.ID.fullmatch(reply.headers['x-request-id'])
                if kind=='stream':
                    assert reply.content==b'data: first\n\ndata: [DONE]\n\n'
                times.append(time.perf_counter()-scheduled)
        await asyncio.gather(*(one(index) for index in range(samples)))
        elapsed = time.perf_counter()-began
    health = sink.health() if sink else None
    if sink:
        await asyncio.to_thread(sink.close)
    log.db.close()
    return {'count':len(times), 'p50':statistics.median(times), 'p95':sorted(times)[int(len(times)*.95)-1],
            'per_second':len(times)/elapsed, 'offered_per_second':rate, 'health':health}


async def qualify(args):
    """Alternate controls and instrumented runs; fail budgets without relaxing them."""
    result = {'basis':'Isolated HTTP/CPU-handler/tool fixtures; matched offered load, not saturation capacity.',
              'cases':[], 'passed':True}
    with tempfile.TemporaryDirectory(prefix='suite-observation-qualification-') as temp:
        root=Path(temp)
        for kind in ('api', 'cpu', 'stream', 'tool', 'reject'):
            for concurrency in (1,8,32):
                runs={False:[], True:[]}
                for repeat in range(args.repeats):
                    for enabled in ((False,True) if repeat%2==0 else (True,False)):
                        folder=root/f'{kind}-{concurrency}-{repeat}-{enabled}'
                        folder.mkdir()
                        runs[enabled].append(await measure(folder,kind,concurrency,enabled,args.samples,args.rate))
                        gc.collect()
                summary={}
                for enabled in (False,True):
                    summary['enabled' if enabled else 'control']={
                        key:statistics.median(r[key] for r in runs[enabled]) for key in ('p50','p95','per_second')}
                control, enabled=summary['control'], summary['enabled']
                passed=(enabled['per_second']>=control['per_second']*.95 and
                        enabled['p95']-control['p95']<=max(.002,control['p95']*.05) and
                        all(r['health']['high_water_bytes']<=8*1024*1024 and not r['health']['dropped'] for r in runs[True]))
                result['cases'].append({'kind':kind,'concurrency':concurrency,**summary,'passed':passed,'runs':runs[True]})
                result['passed'] &= passed
                print(kind, concurrency, 'PASS' if passed else 'FAIL', flush=True)
    result['peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    return result['passed']


def main():
    """Write a machine-readable qualification artifact and return a truthful gate result."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=80)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--rate',type=float,default=100)
    args=parser.parse_args()
    if not 20<=args.samples<=10000 or not 1<=args.repeats<=10 or not 1<=args.rate<=1000:
        parser.error('Use 20–10000 samples, 1–10 repeats and 1–1000 offered calls per second.')
    raise SystemExit(0 if asyncio.run(qualify(args)) else 1)


if __name__=='__main__':
    main()
