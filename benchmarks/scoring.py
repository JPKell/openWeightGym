"""Safe editable score formulas and arithmetic shared by benchmark summaries."""

import ast
import math
import statistics

MODEL_VALUES = {'parameter_count', 'trained_context', 'layers', 'configured_context', 'gpu_vram_mib'}


def formula_tree(text: str, names: set[str]) -> ast.Expression:
    """Accept short arithmetic over named measurements, never executable Python."""
    if len(text) > 500:
        raise ValueError('The score calculation is too long.')
    try:
        tree = ast.parse(text, mode='eval')
    except SyntaxError:
        raise ValueError('Use a calculation such as max_context_tokens * parameter_count.') from None
    nodes = list(ast.walk(tree))
    if len(nodes) > 80:
        raise ValueError('Use a shorter score calculation.')
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Constant, ast.Name, ast.Load)
    for node in nodes:
        if not isinstance(node, allowed):
            raise ValueError('Use numbers, recorded values, parentheses and + - * /.')
        if isinstance(node, ast.Name) and node.id not in names:
            raise ValueError(f'No recorded value named {node.id}.')
        if isinstance(node, ast.Constant) and (type(node.value) not in (int, float) or not math.isfinite(node.value)):
            raise ValueError('Constants must be finite numbers.')
    return tree


def calculate(text: str, values: dict) -> float | None:
    """Evaluate checked arithmetic. Missing operands, zero divisors and overflow are unavailable."""
    def value(node):
        """Resolve one configured scoring input from the measured sample."""
        if isinstance(node, ast.Expression):
            return value(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            found = values.get(node.id)
            if type(found) not in (int, float):
                raise ValueError('unavailable')
            return float(found)
        if isinstance(node, ast.UnaryOp):
            return -value(node.operand) if isinstance(node.op, ast.USub) else value(node.operand)
        a, b = value(node.left), value(node.right)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        return a / b
    try:
        answer = value(formula_tree(text, set(values)))
        return answer if math.isfinite(answer) else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def medians(samples: list[dict]) -> dict:
    """Combine repeated samples at one chosen size, keeping the repetition count."""
    keys = {key for sample in samples for key, amount in sample.items() if type(amount) in (int, float)}
    return {key: statistics.median([sample[key] for sample in samples if type(sample.get(key)) in (int, float)]) for key in keys} | {'repetitions': len(samples)}
