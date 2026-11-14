"""Render three coordinated Collections / collection / Workflows proposals offline."""

import argparse
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def samples():
    cases = [
        ('procurement', 'Supplier due diligence', 'Procurement', 'Review supplier terms, risk and approval requirements.',
         'sensitive', 'Ready', 'What approval is needed for a supplier contract over $50,000?',
         'Finance and Legal must approve contracts above $50,000 before signature.',
         'Contracts with a total value above $50,000 require written approval from Finance and Legal before signature.',
         'Finance must approve the contract before signature.', 'supplier-policy.pdf', 7),
        ('finance', 'Invoice & payment controls', 'Finance', 'Check payment terms, exceptions and invoice handling.',
         'sensitive', 'Ready', 'When must an invoice with a disputed line item be paid?',
         'Pay undisputed items within 30 days; hold disputed items until resolution.',
         'Undisputed invoice items are payable within 30 days of receipt. Disputed items are held until the dispute is resolved.',
         'Hold the entire invoice until the dispute is resolved.', 'payment-policy.pdf', 4),
        ('people', 'Employee onboarding', 'People operations', 'Retrieve the right policy and explain the next step.',
         'normal', 'Indexing', 'When does a new employee become eligible for the equipment allowance?',
         'After completing 30 calendar days of employment.',
         'Employees become eligible for the equipment allowance after completing 30 calendar days of employment.',
         'New employees are eligible immediately.', 'onboarding-guide.pdf', 6),
        ('compliance', 'Records retention', 'Compliance', 'Find retention periods, exceptions and source citations.',
         'restricted', 'Needs attention', 'How long must signed supplier contracts be retained?',
         'Seven years after contract expiry, unless a legal hold applies.',
         'Retain signed supplier contracts for seven years after expiry. An active legal hold overrides the scheduled disposal date.',
         'Retain signed contracts for seven years after signing.', 'records-policy.pdf', 3),
        ('support', 'Customer service handbook', 'Customer operations', 'Test answers to common customer policy questions.',
         'normal', 'Ready', 'Can a customer return a personalised item that is not faulty?',
         'No. Personalised items are returnable only when faulty.',
         'Personalised items cannot be returned for a change of mind. Faulty personalised items remain eligible for a return.',
         'Yes, within 30 days of purchase.', 'returns-handbook.pdf', 5),
    ]
    collections = []
    for i, (id_, title, domain, description, level, state, question, expected, excerpt, alternate, source, page) in enumerate(cases):
        documents = [dict(id=f'{id_}-d{n}', name=name, state='Ready', chunks=chunks, page=page,
                          text=excerpt if n == 0 else f'{title}: {description} Apply the policy to the stated business scenario and retain the original source reference.')
                     for n, (name, chunks) in enumerate([(source, 36), ('operating-procedure.md', 18), ('business-scenarios.csv', 12), ('exceptions-register.txt', 8)])]
        if state == 'Indexing':
            documents[-1].update(state='Indexing', chunks=0)
        if state == 'Needs attention':
            documents[-1].update(state='Failed', chunks=0)
        questions = [dict(id='approval', question=question, expected=expected, excerpt=excerpt,
                          source=source, page=page, alternate=alternate),
                     dict(id='missing', question='Who is the named deputy approver for an urgent exception?',
                          expected='The supplied sources do not name a deputy approver. The answer should say that this information is unavailable.',
                          excerpt='Exceptions must be recorded and approved under the applicable procedure. No deputy approver is named in this source.',
                          source='operating-procedure.md', page=None,
                          alternate='The deputy approver is the operations manager.'),
                     dict(id='exception', question='Does an urgent request remove the requirement to record an exception?',
                          expected='No. Urgent requests still require a recorded exception and the applicable approval.',
                          excerpt='Urgent requests follow the same exception recording and approval requirements as standard requests.',
                          source='exceptions-register.txt', page=None,
                          alternate='An urgent request can proceed without a recorded exception.')]
        collections.append(dict(id=id_, title=title, domain=domain, description=description, level=level,
                                state=state, documents=documents, chunks=sum(d['chunks'] for d in documents),
                                embedder='nomic-embed-text-v1.5', dimensions=768, revision=12+i,
                                questions=questions))
    workflows = [dict(id='supplier-brief', title='Supplier approval briefing', collection='procurement', state='review_research',
                      model='Qwen3-32B', draft_model='Qwen3-14B', prompt='Evidence-first research', version=2),
                 dict(id='payment-note', title='Payment exception note', collection='finance', state='ready_research',
                      model='Qwen3-14B', draft_model='Qwen3-14B', prompt='Evidence-first research', version=2),
                 dict(id='retention-summary', title='Contract retention summary', collection='compliance', state='failed_draft',
                      model='Qwen3-32B', draft_model='Qwen3-14B', prompt='Policy research', version=1),
                 dict(id='return-reply', title='Personalised returns response', collection='support', state='completed',
                      model='Qwen3-32B', draft_model='Qwen3-32B', prompt='Evidence-first research', version=2)]
    return dict(collections=collections, workflows=workflows,
                models=['Qwen3-32B', 'Qwen3-14B'], levels=['normal', 'sensitive', 'restricted'])


def render():
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    static = ROOT / 'weightroom/static'
    return pages.get_template('collection_designs.html').render(
        css='\n'.join((static / name).read_text() for name in
                      ('tabler.min.css', 'style.css', 'themes.css', 'evaluation-studio.css', 'collection-designs.css')),
        theme_javascript=(static / 'themes.js').read_text(),
        javascript=(static / 'collection-designs.js').read_text(), data=samples())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/collection-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
