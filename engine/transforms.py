"""
engine/transforms.py — step-type vocabulary and the generic,
domain-blind value casters/shape-adapters used by EXTRACT_TRANSFORM.
Zero knowledge of any specific contract, field name, or tenant —
everything here operates on shapes and type strings only.
"""
import json
from enum import Enum
from typing import Any, Dict, List, Optional

class StepType(str, Enum):
    TRANSFORM = "EXTRACT_TRANSFORM"
    VALIDATE = "VALIDATE_BOUNDARY"
    CONDITION = "EVALUATE_CONDITION"
    MATRIX = "DECISION_MATRIX"
    DISPATCH = "HTTP_DISPATCH"
    RENDER = "RENDER_ARTIFACT"


_TERMINAL_MARKERS = {"END", "DLQ_REJECT"}

def _json_cast(v: Any) -> Any:
    """Fixes a common upstream-payload class of bug: a nested object/array
    field sometimes arrives as a raw JSON-encoded string rather than
    already-parsed JSON. JMESPath queries against a string silently return
    None instead of erroring, which is exactly the failure mode that makes
    a downstream schema error ("None is not of type ...") opaque instead
    of pointing at the real cause. No-ops if the value is already parsed."""
    return json.loads(v) if isinstance(v, str) else v


def _flatten_nested_keyed_array(
    source_array: Any, group_name: str, prefix: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """Kernel Space Isolation-safe structural transform: operates on ONE
    generic nested shape only -- a named group inside an array of
    {name, value} objects, whose OWN value is itself an array of rows,
    each row an array of {name, value, ext?} objects (the same recursive
    shape as the outer array). This function knows nothing about what any
    specific field NAME means or which contract/tenant is calling it --
    that is what makes it safe to live in the kernel rather than in a
    per-contract declaration: it is generic structural reshaping, not
    business semantics. Any source whose nested-array shape differs from
    this needs either a plain JMESPath mapping or a separate, equally
    generic shape-adapter -- this function is documented as ONE adapter
    among potentially several, not the only supported shape.

    Replaces hand-written JMESPath object-comprehension expressions of the
    form "source[?name=='G'].value | [0][*].{\"G_child1\": [?name=='child1'].value | [0], ...}"
    -- a single unbroken expression that grows by one more "[?name==...]"
    clause per child field -- with one declarative call:
      {"nested_array_flatten": "G"}
    covering an arbitrary number of child fields with zero per-field
    enumeration, because it flattens by each child's OWN 'name' key rather
    than a hand-maintained list of expected names.

    Naming convention: every child becomes "{prefix}_{child_name}". A
    child that also carries .ext.currency additionally emits a sibling
    "{prefix}_{child_name}-Currency" key -- a generic amount/currency-pair
    structural convention, not tied to any specific source format.

    Returns None (not []) when the named group is genuinely absent, so
    `| default([])` in a render template behaves identically to a plain
    JMESPath miss.
    """
    if not isinstance(source_array, list):
        return None
    group = next((w for w in source_array if isinstance(w, dict) and w.get("name") == group_name), None)
    if group is None or not isinstance(group.get("value"), list):
        return None

    effective_prefix = prefix or group_name
    rows: List[Dict[str, Any]] = []
    for row in group["value"]:
        flat: Dict[str, Any] = {}
        if isinstance(row, list):
            for child in row:
                if not isinstance(child, dict) or "name" not in child:
                    continue
                key = f"{effective_prefix}_{child['name']}"
                flat[key] = child.get("value")
                ext = child.get("ext")
                if isinstance(ext, dict) and "currency" in ext:
                    flat[f"{key}-Currency"] = ext["currency"]
        # A malformed row (not a list) still occupies its position as an
        # empty dict -- DROPPING it instead would shift every subsequent
        # row's array index, silently re-creating the bug #6 index-
        # alignment failure this whole mechanism exists to prevent.
        rows.append(flat)
    return rows


_CASTERS = {
    "int": int,
    "float": float,
    "str": str,
    "bool": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes"),
    "json": _json_cast,
}

_DEFAULT_BOOL_TRUTHY = ("true", "1", "yes")


def _resolve_caster(cast_spec: Any):
    """Directive 1 patch: the simple string form ("bool", "float", ...)
    keeps today's exact behavior -- including the English-only truthy set
    above -- for every contract that doesn't ask for anything else. The
    object form ({"type": "bool", "truthy": [...]}) lets a specific
    contract declare its OWN locale's truthy strings (e.g. Vietnamese
    "có"/"đúng") without the engine hardcoding any business/locale
    knowledge; only 'bool' currently reads an extra option, other types
    fall back to the plain _CASTERS lookup by their 'type' value."""
    if cast_spec is None:
        return None
    if isinstance(cast_spec, str):
        return _CASTERS.get(cast_spec)
    if isinstance(cast_spec, dict):
        cast_type = cast_spec.get("type")
        if cast_type == "bool":
            truthy = tuple(str(s).strip().lower() for s in cast_spec.get("truthy", _DEFAULT_BOOL_TRUTHY))
            return lambda v: v if isinstance(v, bool) else str(v).strip().lower() in truthy
        return _CASTERS.get(cast_type)
    return None
