"""Full pinned IFEval dataset and official strict/loose checkers, run entirely locally."""

import hashlib
import json
from pathlib import Path
from ... import quality

HERE=Path(__file__).parent
SOURCE=json.loads((HERE/'source.json').read_text())
DEFINITION=quality.definition('ifeval','IFEval','Follow verifiable instructions in the full official IFEval dataset.',
    'All 541 official prompts with strict and loose prompt/instruction accuracy. Pinned Google Research checkers, fixed language-detection seed, bundled sentence tokenizer. No model judge and no response grammar.',
    [('prompt_strict_accuracy','Strict prompts','%'),('instruction_strict_accuracy','Strict instructions','%'),
     ('prompt_loose_accuracy','Loose prompts','%'),('instruction_loose_accuracy','Loose instructions','%')])
DEFINITION['formula']='prompt_strict_accuracy'
PROTOCOL={**quality.PROTOCOL,'version':3,'dataset':'ifeval','upstream_commit':SOURCE['commit'],
          'dataset_sha256':SOURCE['upstream_sha256']['input_data.jsonl'],'checker_seed':0,'cases':541,'outputs':quality.OUTPUTS,
          'tokenizer_sha256':SOURCE['upstream_sha256']['punkt_tab.zip'],
          'dependencies':{'nltk':'3.10.3','langdetect':'1.0.9','immutabledict':'4.2.2','absl-py':'2.3.1'}}


def cases(limit):
    """Run every upstream prompt unchanged; save official instruction IDs and arguments."""
    result=[]
    data=(HERE/'input_data.jsonl').read_bytes()
    if hashlib.sha256(data).hexdigest()!=PROTOCOL['dataset_sha256']:
        raise ValueError('The pinned IFEval dataset checksum changed.')
    for line in data.splitlines():
        item=json.loads(line)
        result.append({'id':f'ifeval-{item["key"]}','category':'ifeval','context':limit,'inputs':0,
                       'outputs':min(quality.OUTPUTS,limit-256),'request':{'question':item['prompt']},'expected':item})
    return result


def prepare():
    """Load pinned local dependencies and tokenizer data before starting costly inference."""
    from importlib.metadata import PackageNotFoundError, version
    for name, expected in PROTOCOL['dependencies'].items():
        try:
            installed = version(name)
        except PackageNotFoundError:
            raise ValueError(f'IFEval requires {name}=={expected}; install openWeightGym benchmark dependencies.') from None
        if installed != expected:
            raise ValueError(f'IFEval requires {name}=={expected}; found {installed}. Install openWeightGym benchmark dependencies.')
    import nltk
    from langdetect import DetectorFactory
    path=str(HERE/'vendor'/'nltk_data')
    if path not in nltk.data.path:
        nltk.data.path.insert(0,path)
    DetectorFactory.seed=0
    nltk.sent_tokenize('One sentence. Another sentence.')


def grade(case,answer):
    """Use upstream evaluators with seeded checkers and no answer rewriting by the grader."""
    import random
    from .vendor import evaluation_lib, instructions, instructions_util
    prepare()
    item=case['expected']
    # The official data supplies constraints; seed any checker defaults without changing global RNG state.
    instructions.random=random.Random(item['key'])
    instructions_util.random=random.Random(item['key'])
    inp=evaluation_lib.InputExample(**item)
    strict=evaluation_lib.test_instruction_following_strict(inp,{inp.prompt:answer})
    loose=evaluation_lib.test_instruction_following_loose(inp,{inp.prompt:answer})
    checks=[{'field':name,'arguments':item['kwargs'][i],'passed':strict.follow_instruction_list[i],
             'loose_passed':loose.follow_instruction_list[i]} for i,name in enumerate(item['instruction_id_list'])]
    return {'correct':sum(strict.follow_instruction_list),'checks':len(checks),'passed':strict.follow_all_instructions,
            'loose_correct':sum(loose.follow_instruction_list),'loose_passed':loose.follow_all_instructions},checks


async def measure(worker,run,ceiling):
    """Run the instruction following protocol through the existing benchmark probe."""
    prepare()
    await quality.measure(worker,run,ceiling,PROTOCOL,cases,grade)


def summarize(samples):
    """Summarize retained instruction following samples and supported scores."""
    result=quality.summarize(samples)
    if samples:
        count=sum(s['values'].get('checks',0) for s in samples)
        result.update(prompt_strict_accuracy=result['case_accuracy'],instruction_strict_accuracy=result['accuracy'],
                      prompt_loose_accuracy=100*sum(s['values'].get('loose_passed',False) for s in samples)/len(samples),
                      instruction_loose_accuracy=100*sum(s['values'].get('loose_correct',0) for s in samples)/count if count else 0)
    return result
