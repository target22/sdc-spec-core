"""
filters/__init__.py — Central registry.

api_engine.py's entire integration with this package is one line:

    from filters import CUSTOM_FILTERS
    jinja_env.filters.update(CUSTOM_FILTERS)

Adding a new filter means adding a function + an EXPORTS entry in the
right category module below -- never touching this file's aggregation
logic, and never touching the engine boot sequence in api_engine.py.

New category modules are added the same way every existing one was:
create the file, give it its own EXPORTS dict, import it here, and
fold it into CUSTOM_FILTERS. Do not add a single-function file for a
one-off filter -- extend the closest matching category module, or add
a new category module only once a real second function for that
category exists.
"""
from . import datetime_filters
from . import structural_filters

CUSTOM_FILTERS = {
    **datetime_filters.EXPORTS,
    **structural_filters.EXPORTS,
}

# Informational only -- not an enforced ceiling. 3 filters today, nowhere
# near a threshold worth gating in code; revisit categorization if/when
# this genuinely grows unwieldy, not on account of a specific number.
FILTER_COUNT = len(CUSTOM_FILTERS)
