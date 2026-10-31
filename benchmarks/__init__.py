"""Built-in benchmarks. Add a benchmark module here to expose it in the API and worker.

Each module owns DEFINITION, PROTOCOL, async measure(worker, run, ceiling), and summarize(samples).
A quality module also owns grade(case, answer). PROTOCOL['version'] is raised when how a model is asked
changes, which marks older results as due for a rerun; a missing one counts as 1.
The worker calls LoadCoach over HTTP; store and scoring own durable evidence and formulas.
"""

from .suites import context, speed, load, json_schema, extraction, arithmetic, retrieval, code_retrieval, ifeval, tool_use, tool_recovery
from .suites import embedding_context, embedding_speed, embedding_load
from .suites import abstention, tables, turns, reasoning, hierarchy

MODULES = {module.DEFINITION['id']: module for module in (context, speed, load, json_schema, extraction, arithmetic, retrieval, code_retrieval, ifeval, tool_use, tool_recovery, embedding_context, embedding_speed, embedding_load, abstention, tables, turns, reasoning, hierarchy)}
# Record the actual planner/checker code with the run, including shared deterministic grading.
from pathlib import Path
import hashlib
for module in (json_schema, extraction, arithmetic, retrieval, code_retrieval, ifeval, tool_use, tool_recovery, abstention, tables, turns, reasoning, hierarchy):
    sources = [Path(__file__).with_name('quality.py'), Path(module.__file__)]
    if module is ifeval:
        sources += sorted((Path(module.__file__).parent/'vendor').glob('*.py'))
    if module in (tool_use, tool_recovery):
        sources += [Path(__file__).with_name('tooling.py'), Path(__file__).with_name('worker.py')]
    if module is hierarchy:
        sources += [Path(__file__).with_name('worker.py')]
    module.PROTOCOL['implementation_sha256'] = hashlib.sha256(b''.join(path.read_bytes() for path in sources)).hexdigest()
# A benchmark's version is in its definition, so that a result made under an older one can be told from a current one.
for module in MODULES.values():
    module.DEFINITION['version'] = module.PROTOCOL.get('version', 1)
BENCHMARKS = [module.DEFINITION for module in MODULES.values()]

TEXT = ('A benchmark records observations under repeatable conditions. A small garden contains cedar trees, '
        'blue flowers and a stone path. Measurements should distinguish input processing from output generation. '
        'Explain the next observation clearly, using complete sentences. ')
