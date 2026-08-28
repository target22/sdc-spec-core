"""
engine/boundary.py — TZIMTZUM GUARD. Upload-time contract integrity
checks: DECISION_MATRIX rule shape, nested_array_flatten mapping
shape, DAG well-formedness (unknown step types, dangling pointers),
the schema/mapping Sync Guard, and the optional whole-contract
canonical-key/route_tag drift guard. Everything here runs once, at
/v1/admin/contracts upload — never per request.
"""
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from engine.condition_evaluator import ConditionError, dry_run_condition
from engine.transforms import StepType, _TERMINAL_MARKERS

def _validate_matrix_step_shape(step: dict, step_id: str, label: str) -> None:
    rules = step.get("rules")
    if not isinstance(rules, list) or not rules:
        raise HTTPException(
            status_code=400,
            detail=f"[{label}] DECISION_MATRIX step '{step_id}' must define a non-empty 'rules' array",
        )
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict) or "condition" not in rule or "route_tag" not in rule:
            raise HTTPException(
                status_code=400,
                detail=f"[{label}] DECISION_MATRIX step '{step_id}' rule #{idx} must be an object with 'condition' and 'route_tag'",
            )
        if not isinstance(rule["route_tag"], str) or not rule["route_tag"].strip():
            raise HTTPException(
                status_code=400,
                detail=f"[{label}] DECISION_MATRIX step '{step_id}' rule #{idx} 'route_tag' must be a non-empty string",
            )
        try:
            dry_run_condition(rule["condition"])
        except ConditionError as e:
            raise HTTPException(
                status_code=400,
                detail=f"[{label}] DECISION_MATRIX step '{step_id}' rule #{idx} condition rejected: {e}",
            )
    default_route = step.get("default_route")
    if not isinstance(default_route, str) or not default_route.strip():
        raise HTTPException(
            status_code=400,
            detail=f"[{label}] DECISION_MATRIX step '{step_id}' must define a non-empty 'default_route'",
        )


def _validate_transform_step_shape(step: dict, step_id: str, label: str) -> None:
    """Upload-time check for the nested_array_flatten directive -- same
    rigor already applied to DECISION_MATRIX rules, now extended to this
    construct so a malformed contract fails at /v1/admin/contracts upload,
    not on the first real request."""
    for target_key, query in step.get("mapping", {}).items():
        if not isinstance(query, dict):
            continue
        if "nested_array_flatten" not in query:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"[{label}] Step '{step_id}' mapping key '{target_key}' is an object but "
                    f"missing the required 'nested_array_flatten' key. The only supported object-shaped "
                    f"mapping directive today is {{\"nested_array_flatten\": \"<source group name>\", "
                    f"\"prefix\": \"<optional>\"}}."
                ),
            )
        group = query["nested_array_flatten"]
        if not isinstance(group, str) or not group.strip():
            raise HTTPException(
                status_code=400,
                detail=f"[{label}] Step '{step_id}' mapping key '{target_key}': 'nested_array_flatten' must be a non-empty string",
            )
        prefix = query.get("prefix")
        if prefix is not None and (not isinstance(prefix, str) or not prefix.strip()):
            raise HTTPException(
                status_code=400,
                detail=f"[{label}] Step '{step_id}' mapping key '{target_key}': 'prefix', if given, must be a non-empty string",
            )


def _validate_single_stage_dag(dag: Any, entry: Any, label: str) -> None:
    """Rejects a stage if it is not a well-formed DAG: missing/invalid dag
    object, missing/invalid entry_step, unknown step types, malformed
    DECISION_MATRIX rules, or pointers (next/on_pass/on_fail/on_true/
    on_false) to step_ids that don't exist."""
    if not isinstance(dag, dict) or not dag:
        raise HTTPException(
            status_code=400,
            detail=f"[{label}] missing or empty 'dag' object (map of step_id -> step definition)",
        )
    if not entry:
        raise HTTPException(status_code=400, detail=f"[{label}] missing 'entry_step'")
    if entry not in dag:
        raise HTTPException(status_code=400, detail=f"[{label}] entry_step '{entry}' is not defined in 'dag'")

    valid_types = {t.value for t in StepType}
    pointer_fields = ("next", "on_pass", "on_fail", "on_true", "on_false")

    for step_id, step in dag.items():
        step_type = step.get("type")
        if step_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail=f"[{label}] Step '{step_id}' has unknown type '{step_type}'. Must be one of {sorted(valid_types)}",
            )
        if step_type == StepType.MATRIX.value:
            _validate_matrix_step_shape(step, step_id, label)
        if step_type == StepType.TRANSFORM.value:
            _validate_transform_step_shape(step, step_id, label)
        for field in pointer_fields:
            target = step.get(field)
            if target is not None and target not in dag and target not in _TERMINAL_MARKERS:
                raise HTTPException(
                    status_code=400,
                    detail=f"[{label}] Step '{step_id}' field '{field}' points to undefined step '{target}'",
                )


def _validate_dag_structure(contract: dict) -> None:
    """Entry point for structural validation. `contract` must already be
    normalized (run through _adapt_contract first) — 'stages' is assumed
    present unconditionally. Legacy-shape branching used to live here too;
    it's now centralized in _adapt_contract so this function has exactly
    one job: is every stage's DAG well-formed."""
    stages = contract["stages"]
    if not isinstance(stages, dict) or not stages:
        raise HTTPException(
            status_code=400,
            detail="'stages' must be a non-empty object (map of stage_name -> {entry_step, dag})",
        )
    for stage_name, stage in stages.items():
        if not isinstance(stage, dict):
            raise HTTPException(status_code=400, detail=f"Stage '{stage_name}' must be an object")
        _validate_single_stage_dag(stage.get("dag"), stage.get("entry_step"), f"stage:{stage_name}")


def _validate_schema_mapping_sync_for_dag(dag: dict, entry_step: Any, label: str) -> None:
    """
    Sync Guard — the fix for the additionalProperties:false trap, run
    per-stage. Walks the stage's primary path (next / on_pass) from its
    entry_step, accumulating EXTRACT_TRANSFORM output keys (and the
    'route_tag' key injected by DECISION_MATRIX), until it reaches the
    first VALIDATE_BOUNDARY step. If that step's schema sets
    additionalProperties:false, every accumulated key must be declared in
    schema.properties — otherwise the contract would reject every payload
    it successfully extracted, at every future runtime call.

    Best-effort static check of the primary (non-branching) path; it does
    not attempt to prove correctness across every conditional branch.
    """
    accumulated_keys: set = set()
    visited: set = set()
    current_id = entry_step

    while current_id and current_id not in _TERMINAL_MARKERS:
        if current_id in visited:
            break  # cycles are rejected by _validate_single_stage_dag's caller path
        visited.add(current_id)
        step = dag.get(current_id)
        if step is None:
            break

        if step.get("type") == StepType.TRANSFORM.value:
            # Underscore-prefixed target keys are internal scratch space
            # (same convention as _raw / _render_result): VALIDATE_BOUNDARY
            # never sees them at runtime, so the sync guard must not
            # demand they be declared in schema.properties either.
            accumulated_keys.update(k for k in step.get("mapping", {}).keys() if not k.startswith("_"))
            current_id = step.get("next")
            continue

        if step.get("type") == StepType.MATRIX.value:
            accumulated_keys.add("route_tag")
            current_id = step.get("next")
            continue

        if step.get("type") == StepType.VALIDATE.value:
            schema = step.get("schema", {})
            if schema.get("additionalProperties") is False:
                declared = set(schema.get("properties", {}).keys())
                orphaned = accumulated_keys - declared
                if orphaned:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"[{label}] Schema/Mapping Sync Error at step '{current_id}': "
                            f"additionalProperties is false but extraction produces "
                            f"keys not declared in schema.properties: {sorted(orphaned)}"
                        ),
                    )
            break  # only the first validation boundary on the primary path is checked

        current_id = step.get("next") or step.get("on_true")


def _validate_schema_mapping_sync(contract: dict) -> None:
    """Runs the Sync Guard across every stage. `contract` must already be
    normalized — see _validate_dag_structure docstring."""
    for stage_name, stage in contract["stages"].items():
        _validate_schema_mapping_sync_for_dag(
            stage.get("dag", {}), stage.get("entry_step"), f"stage:{stage_name}"
        )


def _validate_canonical_keys(contract: dict) -> None:
    """Directive 2 patch (Canonical IR). Opt-in, whole-contract version of
    the Sync Guard above: that check only walks ONE stage's primary path
    against ONE VALIDATE_BOUNDARY schema, so it structurally cannot catch
    a field spelled 'customer_name' in stage_1 and 'customer_nam' (typo)
    in stage_2's own re-promotion TRANSFORM step -- two independent
    stages, two independent free-text mappings, nothing connecting them.

    If the contract declares 'canonical_keys', every EXTRACT_TRANSFORM
    step in EVERY stage must write only field names from that set.
    If it declares 'canonical_route_tags', every DECISION_MATRIX rule's
    route_tag and every default_route must be a value from that set.
    Either or both are optional -- a contract that declares neither is
    completely unaffected by this function; this is additive, not a
    breaking change for anything written before it existed.
    """
    key_set = contract.get("canonical_keys")
    tag_set = contract.get("canonical_route_tags")
    if key_set is None and tag_set is None:
        return
    key_set = set(key_set) if key_set is not None else None
    tag_set = set(tag_set) if tag_set is not None else None

    for stage_name, stage in contract["stages"].items():
        for step_id, step in stage.get("dag", {}).items():
            step_type = step.get("type")

            if key_set is not None and step_type == StepType.TRANSFORM.value:
                used = {k for k in step.get("mapping", {}).keys() if not k.startswith("_")}
                orphaned = used - key_set
                if orphaned:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"[stage:{stage_name}] Step '{step_id}' writes keys not declared in "
                            f"'canonical_keys': {sorted(orphaned)}"
                        ),
                    )

            if tag_set is not None and step_type == StepType.MATRIX.value:
                used_tags = {r.get("route_tag") for r in step.get("rules", []) if r.get("route_tag")}
                default_route = step.get("default_route")
                if default_route:
                    used_tags.add(default_route)
                orphaned_tags = used_tags - tag_set
                if orphaned_tags:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"[stage:{stage_name}] DECISION_MATRIX step '{step_id}' produces route_tag(s) "
                            f"not declared in 'canonical_route_tags': {sorted(orphaned_tags)}"
                        ),
                    )
