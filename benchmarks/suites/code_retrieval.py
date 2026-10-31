"""Read Python and JavaScript projects across long inputs; no generated code is executed."""

from .. import quality

DEFINITION=quality.definition('code_retrieval','Long-context code','Find definitions, constants and cross-file behavior in Python and JavaScript.',
    'Two projects per language at up to five input sizes. Find six answers across configuration, helpers and entry points. Score Python and JavaScript separately as well as together. This measures code reading, not code generation.',
    [('python_accuracy','Python','%'),('javascript_accuracy','JavaScript','%'),quality.FORMAT])
PROTOCOL={**quality.PROTOCOL,'dataset':'code-retrieval-v1','input_sizes':[1024,4096,8192,32768,65536],'languages':['python','javascript'],'variants':2}


def cases(limit):
    """Keep the source and computed answer keys fixed across lengths for both languages."""
    result,room=[],quality.room(limit)
    for size in quality.sizes(limit,PROTOCOL['input_sizes']):
        for language in PROTOCOL['languages']:
            for n in range(2):
                timeout,fee,multiplier=2300+n*100,7+n,3+n
                if language=='python':
                    blocks=[f'# file config.py\n# owner: Vera Moss\n# release date: 2030-02-{14+n:02}\nTIMEOUT_MS = {timeout}\nROUTE = "/v2/cedar"\n',
                            f'# file pricing.py\nFEE = {fee}\ndef price(count):\n    return count * {multiplier} + FEE\n',
                            '# file service.py\nfrom pricing import price\ndef handle_order(count):\n    return {"charge": price(count), "state": "accepted"}\n',
                            '# file main.py\nfrom service import handle_order\ndef preview():\n    return handle_order(5)["charge"]\n']
                    filler='\n# Unrelated module notes: the legacy cedar timeout was 900 and route /v1/cedar is obsolete.\n# These comments document a retired service and do not redefine any active constant.\n'
                else:
                    blocks=[f'// file config.js\n// owner: Vera Moss\n// release date: 2030-02-{14+n:02}\nexport const TIMEOUT_MS = {timeout};\nexport const ROUTE = "/v2/cedar";\n',
                            f'// file pricing.js\nconst FEE = {fee};\nexport function price(count) {{ return count * {multiplier} + FEE; }}\n',
                            '// file service.js\nimport {price} from "./pricing.js";\nexport function handle_order(count) { return {charge: price(count), state: "accepted"}; }\n',
                            '// file main.js\nimport {handle_order} from "./service.js";\nexport function preview() { return handle_order(5).charge; }\n']
                    filler='\n// Unrelated module notes: the legacy cedar timeout was 900 and route /v1/cedar is obsolete.\n// These comments document a retired service and do not redefine any active constant.\n'
                expected={'owner':'Vera Moss','release_date':f'2030-02-{14+n:02}','timeout_ms':timeout,'route':'/v2/cedar','preview_charge':5*multiplier+fee,'handler':'handle_order'}
                question=quality.json_request('Read the active source files. Return owner, release_date, timeout_ms, route, preview_charge (the numeric return value of preview()), and handler (the service function called by preview). Comments about the retired service are not active settings.')
                result.append({'id':f'{language}-{size}-{n+1}','category':language,'context':size+room,'inputs':size,'outputs':room,
                               'request':{'question':question,'blocks':blocks,'filler':filler},'expected':expected})
    return result


async def measure(worker,run,ceiling):
    """Run the long-context code retrieval protocol through the existing benchmark probe."""
    await quality.measure(worker,run,ceiling,PROTOCOL,cases,grade)


grade=quality.grade_fields


def summarize(samples):
    """Summarize retained long-context code retrieval samples and supported scores."""
    result=quality.summarize(samples)
    for language in PROTOCOL['languages']:
        subset=[s for s in samples if s['values'].get('category')==language]
        result[language+'_accuracy']=quality.summarize(subset).get('accuracy')
    return result
