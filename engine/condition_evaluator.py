"""
engine/condition_evaluator.py — the whitelist-only AST interpreter for
DECISION_MATRIX conditions. No raw eval()/exec() anywhere in this
module; ast.parse() is a grammar check only, _eval_node is the actual
(and only) computation path, at both upload-time (dry_run_condition)
and request-time (evaluate_condition) — one source of truth for what a
condition string is allowed to do.
"""
import ast
import re
from typing import Any, Dict, List

MAX_CONDITION_LENGTH = 2000

class ConditionError(ValueError):
    """Raised for malformed or disallowed DECISION_MATRIX conditions."""


_STRING_LITERAL_RE = re.compile(r"""('([^'\\]|\\.)*'|"([^"\\]|\\.)*")""")


def _translate_chunk(chunk: str) -> str:
    """JS/JSON-style operator translation. Only ever called on text OUTSIDE
    quoted string spans — see _translate_condition."""
    chunk = re.sub(r"&&", " and ", chunk)
    chunk = re.sub(r"\|\|", " or ", chunk)
    chunk = re.sub(r"!(?!=)", " not ", chunk)
    chunk = re.sub(r"\btrue\b", "True", chunk)
    chunk = re.sub(r"\bfalse\b", "False", chunk)
    chunk = re.sub(r"\bnull\b", "None", chunk)
    return chunk


def _translate_condition(expr: str) -> str:
    """Rewrites contract-authoring syntax (&&, ||, !, true/false/null) into
    Python syntax, without touching the interior of quoted string literals
    — so a rule like status == 'true' still compares against the literal
    string 'true', not the boolean True."""
    parts: List[str] = []
    last_end = 0
    for m in _STRING_LITERAL_RE.finditer(expr):
        parts.append(_translate_chunk(expr[last_end:m.start()]))
        parts.append(m.group(0))  # quoted literal left untouched
        last_end = m.end()
    parts.append(_translate_chunk(expr[last_end:]))
    return "".join(parts).strip()


def _eval_node(node: ast.AST, names: Any) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, names)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise ConditionError(f"Undefined variable '{node.id}' in condition")
        return names[node.id]
    if isinstance(node, ast.List):
        return [_eval_node(el, names) for el in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(el, names) for el in node.elts)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, names)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ConditionError(f"Unsupported unary operator '{type(node.op).__name__}'")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval_node(v, names)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for v in node.values:
                result = _eval_node(v, names)
                if result:
                    return result
            return result
        raise ConditionError(f"Unsupported boolean operator '{type(node.op).__name__}'")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, names)
        right = _eval_node(node.right, names)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.FloorDiv):
            return left // right
        # ast.Pow is deliberately unsupported: an unbounded exponent against
        # Python's arbitrary-precision ints is a trivial CPU/memory DoS.
        raise ConditionError(f"Unsupported binary operator '{type(op).__name__}'")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, names)
        for op, comparator_node in zip(node.ops, node.comparators):
            right = _eval_node(comparator_node, names)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            else:
                raise ConditionError(f"Unsupported comparator '{type(op).__name__}'")
            if not ok:
                return False
            left = right
        return True
    # Anything not explicitly handled above (Call, Attribute, Subscript,
    # Lambda, Dict, Set, comprehensions, Starred, f-strings, walrus, ...)
    # is rejected here.
    raise ConditionError(f"Disallowed expression construct: '{type(node).__name__}'")


def evaluate_condition(raw_condition: str, context: Dict[str, Any]) -> bool:
    """Runtime entrypoint: translates + parses + interprets a DECISION_MATRIX
    rule condition against the current DAG context. Never calls eval/exec."""
    if not isinstance(raw_condition, str) or not raw_condition.strip():
        raise ConditionError("Condition must be a non-empty string")
    if len(raw_condition) > MAX_CONDITION_LENGTH:
        raise ConditionError(f"Condition exceeds {MAX_CONDITION_LENGTH} character limit")
    translated = _translate_condition(raw_condition)
    try:
        tree = ast.parse(translated, mode="eval")
    except SyntaxError as e:
        raise ConditionError(f"Syntax error in condition: {e}")
    return bool(_eval_node(tree, context))


class _AnyValue:
    """Dummy sentinel used only to dry-run a condition at contract-upload
    time, before any real payload exists. Answers every comparison,
    membership, arithmetic, and boolean operation so the interpreter can
    fully walk (and thus validate) the expression without real data."""

    def __eq__(self, other): return True
    def __ne__(self, other): return True
    def __lt__(self, other): return True
    def __le__(self, other): return True
    def __gt__(self, other): return True
    def __ge__(self, other): return True
    def __contains__(self, item): return True
    def __bool__(self): return True
    def __add__(self, other): return self
    def __radd__(self, other): return self
    def __sub__(self, other): return self
    def __mul__(self, other): return self
    def __truediv__(self, other): return self
    def __mod__(self, other): return self
    def __floordiv__(self, other): return self
    def __neg__(self): return self


_ANY_VALUE = _AnyValue()


class _AnyNames:
    def __contains__(self, key: str) -> bool:
        return True

    def __getitem__(self, key: str) -> Any:
        return _ANY_VALUE


def dry_run_condition(raw_condition: str) -> None:
    """Upload-time check, called from _validate_matrix_step_shape. Raises
    ConditionError on syntax errors or disallowed constructs, using the
    exact same interpreter as runtime (single source of truth)."""
    evaluate_condition(raw_condition, _AnyNames())
