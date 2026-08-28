"""
filters/structural_filters.py — Egress-side shape adapters for
destination-system field types that require a specific nested JSON
structure rather than a bare scalar.

Domain-blind by construction: each function encodes a generic TARGET
SHAPE convention (e.g. "a Link-type field wants {link,text} and
encodes absence as bare null"), never a specific field NAME, contract,
or tenant. Any contract talking to a destination with the same
field-shape convention can reuse these -- nothing here is hardcoded to
Lark Base in principle, even though today's only caller is.

Replaces the jlink()/link_field() Jinja macros that were previously
duplicated (and had already drifted apart in name/signature) across
individual logic_rule_output.j2 files. One Python implementation now,
called identically from every contract's template.
"""
from typing import Any, Dict, List, Optional


def as_link_field(v: Any, text: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Canonical {"link": ..., "text": ...} shape for Link/URL-type
    destination fields. Absence must render as bare JSON null, not a
    populated-but-empty object -- {"link": null, "text": null} is
    rejected outright by at least one known destination system
    (LinkFieldConvFail / URLFieldConvFail on Lark Base) rather than
    accepted as "no value". `text` lets link and display text differ
    (e.g. an attachment's filename vs. its URL); omit it when link and
    text are the same value."""
    if v is None or v == "":
        return None
    return {"link": v, "text": text if text is not None else v}


def as_user_field(v: Any) -> List[Dict[str, Any]]:
    """Canonical [{"id": ...}, ...] shape for User-type destination
    fields. Accepts a single id or a list of ids; None/empty collapses
    to an empty list (the destination's encoding of "no assignee"),
    never [{"id": None}]."""
    if v is None or v == "":
        return []
    ids = v if isinstance(v, list) else [v]
    return [{"id": i} for i in ids]


EXPORTS = {
    "as_link_field": as_link_field,
    "as_user_field": as_user_field,
}
