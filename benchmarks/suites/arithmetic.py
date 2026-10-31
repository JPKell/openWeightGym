"""Seeded multi-step arithmetic with exact money, table, unit, percentage and date keys."""

import csv
import io
from datetime import date, timedelta
from fractions import Fraction

from .. import quality

SEED = 20261014
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
DEFINITION = quality.definition(
    'arithmetic', 'Arithmetic & data', 'Calculate exact multi-step answers from quantities and records.',
    'Forty seeded cases, eight each of multi-step purchases, 30-row CSV queries, chained unit '
    'conversions, percentage changes and calendar calculations. There are 128 independently '
    'graded fields. Keys use exact fractions and dates; money is returned in cents and percent '
    'changes in tenths of a percent. Native unconstrained answers retain their raw output and '
    'cap flags; correct JSON may be wrapped in prose or reasoning.', [quality.FORMAT])
PROTOCOL = {**quality.PROTOCOL, 'version': 3, 'dataset': 'arithmetic-v3', 'cases': 40, 'seed': SEED}


def integer(value):
    """Reject a generated question whose requested answer would require a rounding rule."""
    value = Fraction(value)
    if value.denominator != 1:
        raise ValueError('Arithmetic case has a nonintegral answer.')
    return value.numerator


def money(cents):
    """Render exact cents as dollars without floating-point formatting."""
    return f'${cents // 100}.{cents % 100:02}'


def purchases(source, index):
    """Discount items, tax the discounted amount, add shipping, split and subtract a budget."""
    rng = source.random
    person, place = source.person(), source.place()
    people = rng.choice((2, 4, 5, 8))
    quantity, price = people * rng.choice((4, 8, 12)), source.number(12, 90) * 100
    discount, tax = rng.choice((10, 20, 25)), rng.choice((5, 10, 15))
    shipping = people * rng.randrange(25, 180)
    subtotal = quantity * price
    billed = Fraction(subtotal) * (1 - Fraction(discount, 100)) * (1 + Fraction(tax, 100)) + shipping
    share = integer(billed / people)
    budget = share + source.number(100, 3500)
    question = (f'{person} organizes a purchase for {place}. {people} people buy {quantity} boxes '
                f'at {money(price)} each. Discount only the items by {discount}%, then tax only '
                f'the discounted items at {tax}%. Add {money(shipping)} shipping, which is neither '
                f'discounted nor taxed. Split the final bill equally among the {people} people. '
                f'Each person has a budget of {money(budget)}. Return subtotal_cents (items before '
                'discount/tax/shipping), share_cents (one person\'s final share), and left_cents '
                '(one person\'s budget minus that share).')
    facts = dict(people=people, quantity=quantity, price_cents=price, discount=discount,
                 tax=tax, shipping_cents=shipping, budget_cents=budget)
    return question, {'subtotal_cents': subtotal, 'share_cents': share, 'left_cents': budget - share}, facts, [person, place]


def tables(source, index):
    """Query thirty distinct rows; define tie-breaking rather than leaving a winner ambiguous."""
    rng = source.random
    teams = [f'g{index+1}{letter}' for letter in 'ABC']
    rows = [{'row': f'r{index+1}_{n+1:02}', 'team': teams[n % 3],
             'units': rng.randrange(1, 81), 'cents': rng.randrange(35, 901) * 5} for n in range(30)]
    rng.shuffle(rows)
    team = teams[index % 3]
    threshold = sorted(row['cents'] for row in rows if row['team'] == team)[2 + index % 6]
    selected = [row for row in rows if row['team'] == team and row['cents'] >= threshold]
    totals = {name: sum(row['units'] * row['cents'] for row in rows if row['team'] == name) for name in teams}
    highest = min(rows, key=lambda row: (-row['units'] * row['cents'], row['row']))['row']
    winner = min(teams, key=lambda name: (-totals[name], name))
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=('row', 'team', 'units', 'cents'), lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    question = (f'CSV sales ledger (cents is the unit price, not a row total):\n{stream.getvalue()}\n'
                f'Select rows with team={team} AND cents>={threshold}. Return selected_rows '
                '(number selected), selected_units (sum of their units), highest_row (row ID '
                'with the greatest units*cents over the whole ledger), and winning_team '
                '(team with the greatest sum of units*cents over the whole ledger). Break any '
                'tie by the lexicographically smallest row ID or team code, respectively.')
    expected = dict(selected_rows=len(selected), selected_units=sum(row['units'] for row in selected),
                    highest_row=highest, winning_team=winner)
    facts = dict(rows=rows, selected_team=team, threshold_cents=threshold)
    return question, expected, facts, [row['row'] for row in rows] + teams


def conversions(source, index):
    """Combine mixed units and a second conversion or multiplier; all answers are integers."""
    rng = source.random
    available = ('duration_seconds', 'distance_mm', 'mass_grams', 'payment_cents')
    omitted = available[index % len(available)]
    questions, expected, facts = [], {}, {}
    if omitted != 'duration_seconds':
        hours, minutes, seconds = rng.randrange(2, 19), rng.randrange(1, 60), rng.randrange(1, 60)
        questions.append(f'A duration is {hours} hours, {minutes} minutes and {seconds} seconds; convert to duration_seconds.')
        expected['duration_seconds'] = hours * 3600 + minutes * 60 + seconds
        facts['duration'] = [hours, minutes, seconds]
    if omitted != 'distance_mm':
        metres, centimetres, pieces = rng.randrange(2, 90), rng.randrange(1, 100), rng.randrange(2, 8)
        questions.append(f'Each of {pieces} cable pieces is {metres} metres and {centimetres} centimetres long; return their combined distance_mm.')
        expected['distance_mm'] = integer((Fraction(metres) + Fraction(centimetres, 100)) * 1000 * pieces)
        facts['distance'] = [metres, centimetres, pieces]
    if omitted != 'mass_grams':
        kilograms, grams, bags = rng.randrange(2, 35), rng.randrange(1, 1000), rng.randrange(2, 9)
        questions.append(f'Each of {bags} bags weighs {kilograms} kilograms and {grams} grams; return combined mass_grams.')
        expected['mass_grams'] = integer((Fraction(kilograms) + Fraction(grams, 1000)) * 1000 * bags)
        facts['mass'] = [kilograms, grams, bags]
    if omitted != 'payment_cents':
        dollars, cents, payments = rng.randrange(12, 150), rng.randrange(1, 100), rng.randrange(2, 8)
        questions.append(f'There are {payments} payments of {dollars} dollars and {cents} cents each; return combined payment_cents.')
        expected['payment_cents'] = (dollars * 100 + cents) * payments
        facts['payment'] = [dollars, cents, payments]
    return ' '.join(questions), expected, facts, [f'conversion-{index+1}']


def percentages(source, index):
    """Measure signed change, invert a percentage and compound a rise followed by a fall."""
    rng = source.random
    start = source.number(120, 900) * 100
    change = (-25, -20, -10, 5, 15, 25, -15, 20)[index]
    end = integer(Fraction(start) * (1 + Fraction(change, 100)))
    original, rise = source.number(20, 110) * 100, rng.choice((10, 20, 25, 50))
    shown = integer(Fraction(original) * (1 + Fraction(rise, 100)))
    stock, first, second = source.number(920, 1800) * 400, rng.choice((10, 20, 25, 50)), rng.choice((10, 20, 25, 50))
    final = integer(Fraction(stock) * (1 + Fraction(first, 100)) * (1 - Fraction(second, 100)))
    ids = [f'price-{index+1}', f'inverse-{index+1}', f'stock-{index+1}']
    question = (f'For item {ids[0]}, price changed from {money(start)} to {money(end)}. '
                'Return change_tenths_percent: signed percentage change in tenths of a percent '
                '(for example, a 7.5% rise is 75 and a 7.5% fall is -75). '
                f'Another item, {ids[1]}, costs {money(shown)} after a {rise}% rise; return '
                f'original_cents before that rise. Inventory {ids[2]} starts at {stock} units, '
                f'rises by {first}%, then falls by {second}% of the increased inventory. '
                'Return final_units. There is no rounding at either step.')
    expected = dict(change_tenths_percent=integer(Fraction(end - start, start) * 1000),
                    original_cents=original, final_units=final)
    facts = dict(start_cents=start, end_cents=end, shown_cents=shown, rise=rise,
                 stock=stock, first=first, second=second)
    return question, expected, facts, ids


def calendars(source, index):
    """Cross February's end, alternate leap/non-leap years and ask independent date questions."""
    rng = source.random
    year = (2028, 2027, 2032, 2029, 2036, 2030, 2040, 2031)[index]
    start = source.fresh(lambda: date(year, 2, rng.randrange(20, 29)))
    end = source.fresh(lambda: date(year, 3, rng.randrange(3, 20)))
    weekday = source.date()
    anchor = source.date()
    moved = source.fresh(lambda: anchor + timedelta(days=rng.choice((-47, -31, -19, 16, 29, 43))))
    offset = (moved - anchor).days
    question = (f'Return elapsed_days from {start.isoformat()} to {end.isoformat()} '
                '(ordinary date subtraction: exclude the start). Return weekday_name for '
                f'{weekday.isoformat()}, spelled in English. Starting at {anchor.isoformat()}, '
                f'add {offset} calendar days (negative means go backward); return shifted_date '
                'as YYYY-MM-DD. Use the Gregorian calendar.')
    expected = dict(elapsed_days=(end - start).days, weekday_name=WEEKDAYS[weekday.weekday()], shifted_date=moved.isoformat())
    facts = dict(start=start.isoformat(), end=end.isoformat(), weekday=weekday.isoformat(), anchor=anchor.isoformat(), offset=offset)
    return question, expected, facts, [value.isoformat() for value in (start, end, weekday, anchor, moved)]


def cases(limit):
    """Make forty fixed-seed cases; only the transport budgets depend on context size."""
    source = quality.Source(SEED)
    result = []
    for category, make in (('word', purchases), ('table', tables), ('conversion', conversions),
                           ('percent', percentages), ('calendar', calendars)):
        for index in range(8):
            question, expected, facts, entities = make(source, index)
            question = quality.json_request(question) + '\nWrite exactly one final object, then stop.'
            result.append(dict(id=f'{category}-{index+1}', category=category, context=limit, inputs=0,
                               outputs=quality.room(limit), request={'question': question}, expected=expected,
                               source=facts, entities=entities))
    return result


async def measure(worker, run, ceiling):
    """Run the arithmetic and data reasoning protocol through the existing benchmark probe."""
    await quality.measure(worker, run, ceiling, PROTOCOL, cases, grade)


grade = quality.grade_fields
summarize = quality.summarize
