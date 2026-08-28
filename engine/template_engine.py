"""
engine/template_engine.py — JINJA2 HOST. Initializes the shared Jinja
Environment used by both /v1/admin/contracts (syntax check at upload)
and RENDER_ARTIFACT (real render at request time), and auto-registers
CUSTOM_FILTERS from the top-level filters/ package. Nothing here is
contract- or tenant-specific.
"""
from jinja2 import Environment, StrictUndefined

from filters import CUSTOM_FILTERS

jinja_env = Environment(undefined=StrictUndefined, autoescape=False)

# Egress-side utility filters (iso_to_epoch_ms, as_link_field,
# as_user_field, ...) live in filters/ as grouped, domain-blind pure
# functions -- see filters/__init__.py. One aggregate update() call;
# adding a new filter never touches this file again.
jinja_env.filters.update(CUSTOM_FILTERS)
