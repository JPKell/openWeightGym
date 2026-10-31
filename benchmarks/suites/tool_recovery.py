"""Recovery after actual injected responses from deterministic mock tools."""

from .. import tooling

DEFINITION = tooling.definition('tool_recovery', 'Tool recovery',
    'Recover from transient failures, changed identifiers and permanent refusals.')
PROTOCOL = {**tooling.PROTOCOL, 'dataset': 'tool-recovery-v1', 'cases': 12}


def cases(limit):
    """Build deterministic synthetic tool recovery fixtures from the case seed."""
    return tooling.cases(limit, recovery=True)


async def measure(worker, run, ceiling):
    """Run the synthetic tool recovery protocol through the existing benchmark probe."""
    await tooling.measure(worker, run, ceiling, PROTOCOL, cases)


grade = tooling.grade
saved_answer = tooling.answer
summarize = tooling.summarize
breakdown = tooling.breakdown
