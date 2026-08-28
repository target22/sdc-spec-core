"""
engine/ — the Sovereign Core Engine's internals, split by
responsibility. Import order (top has zero internal deps, bottom
depends on everything above it):

  logging_setup -> security -> transforms -> condition_evaluator -> template_engine
      -> dlq -> contracts -> boundary -> payload -> executor -> (main.py)

No cycles: verified by successfully importing every module in this
exact order in isolation (see delivery notes).
"""
