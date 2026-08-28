"""
engine/executor.py — walks a stage's DAG: dispatches each step to its
handler by StepType (_run_step), and drives the full loop with cycle
detection, step-count limiting, and the state-driven response envelope
(_execute_dag). This is the one module that actually EXECUTES a
contract; everything upstream (contracts.py, boundary.py) only
prepares or validates what gets handed in here.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
import jmespath
import jsonschema
from fastapi import HTTPException
from json_logic import jsonLogic

from engine.condition_evaluator import ConditionError, evaluate_condition
from engine.dlq import dlq_log
from engine.template_engine import jinja_env
from engine.transforms import StepType, _TERMINAL_MARKERS, _flatten_nested_keyed_array, _resolve_caster

async def _run_step(
    step: dict,
    step_id: str,
    context: Dict[str, Any],
    customer_id: str,
    stage_name: str,
    http_client: httpx.AsyncClient,
) -> Optional[str]:
    """Executes one DAG step, mutating `context` in place, and returns the
    id of the next step to run ('END' to stop)."""
    try:
        st = StepType(step.get("type"))
    except ValueError:
        dlq_log(customer_id, 500, f"Unknown step type '{step.get('type')}' @ {step_id} [stage:{stage_name}]", context.get("_raw"))
        raise HTTPException(status_code=500, detail=f"Contract Configuration Error: unknown step type at '{step_id}'")

    if st is StepType.TRANSFORM:
        source = context["_raw"] if step.get("source", "raw") == "raw" else context
        cast_map = step.get("cast", {})
        for target_key, query in step.get("mapping", {}).items():
            if isinstance(query, dict) and "nested_array_flatten" in query:
                # Structural shape-adapter directive (replaces deep JMESPath
                # object-comprehension mappings) -- reads the source array
                # from the context key named by 'source' (default
                # '_form_parsed', the conventional scratch-space name for a
                # pre-parsed nested array, same underscore convention as
                # _raw/_render_result). Falls back to None (not a crash) if
                # that key isn't populated yet -- a contract author who
                # forgets to extract it first sees a normal
                # VALIDATE_BOUNDARY failure on the missing field, not an
                # opaque 500.
                value = _flatten_nested_keyed_array(
                    context.get(query.get("source", "_form_parsed")),
                    query["nested_array_flatten"],
                    query.get("prefix"),
                )
            else:
                value = jmespath.search(query, source)
            caster = _resolve_caster(cast_map.get(target_key))
            if caster is not None and value is not None:
                try:
                    value = caster(value)
                except (TypeError, ValueError):
                    pass  # leave uncast; VALIDATE_BOUNDARY will catch the type mismatch
            context[target_key] = value
        return step.get("next", "END")

    if st is StepType.VALIDATE:
        schema = step.get("schema", {})
        instance = {k: v for k, v in context.items() if not k.startswith("_")}
        try:
            jsonschema.validate(instance=instance, schema=schema, format_checker=jsonschema.FormatChecker())
        except jsonschema.ValidationError as e:
            # Surface the exact failing field, not just a generic schema
            # message, in both the DLQ entry and the HTTP 422 body.
            failing_field = ".".join(str(p) for p in e.path) or "(root)"
            # Directive II.3/IV.1 (Zero-PII Logging): e.message can embed
            # the raw failing value verbatim (e.g. enum violations render
            # as "'Nguyen Van A' is not one of [...]") -- that string is
            # NOT run through the payload sanitizer above, so it must never
            # be handed to dlq_log() as-is. The DLQ reason is reconstructed
            # instead from schema-side metadata only (validator name +
            # expected constraint + the ACTUAL VALUE'S TYPE) -- everything
            # a developer needs to diagnose a mapping/type bug, with zero
            # raw customer data. The HTTP 422 response below still uses
            # e.message: that's the caller's own submitted data being
            # echoed back to them, not a disclosure to a new party, so it
            # is out of scope for this directive.
            dlq_reason = (
                f"Schema Violation @ {step_id} [stage:{stage_name}] field='{failing_field}' "
                f"validator='{e.validator}' expected={e.validator_value!r} "
                f"got_type={type(e.instance).__name__}"
            )
            dlq_log(customer_id, 422, dlq_reason, context.get("_raw"))
            raise HTTPException(status_code=422, detail=f"Schema Violation on field '{failing_field}': {e.message}")
        return step.get("on_pass", step.get("next", "END"))

    if st is StepType.CONDITION:
        instance = {k: v for k, v in context.items() if not k.startswith("_")}
        try:
            result = jsonLogic(step.get("logic", {}), instance)
        except Exception as e:
            dlq_log(customer_id, 500, f"EVALUATE_CONDITION failed @ {step_id} [stage:{stage_name}]: {str(e)}", context.get("_raw"))
            raise HTTPException(
                status_code=500,
                detail=f"Contract Configuration Error in condition '{step_id}': {str(e)}",
            )
        return step.get("on_true", "END") if result else step.get("on_false", "END")

    if st is StepType.MATRIX:
        instance = {k: v for k, v in context.items() if not k.startswith("_")}
        matched_tag: Optional[str] = None
        for idx, rule in enumerate(step.get("rules", [])):
            try:
                if evaluate_condition(rule.get("condition", ""), instance):
                    matched_tag = rule.get("route_tag")
                    break
            except ConditionError as e:
                # Upload-time validation should already have caught this;
                # hitting it at runtime means a condition references a
                # field no TRANSFORM step in THIS stage populated. Treat
                # as a config error, not a silent fall-through.
                dlq_log(
                    customer_id, 500,
                    f"DECISION_MATRIX rule #{idx} @ {step_id} [stage:{stage_name}] invalid: {str(e)}",
                    context.get("_raw"),
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Contract Configuration Error in DECISION_MATRIX '{step_id}', rule #{idx}: {str(e)}",
                )
        if matched_tag is None:
            default_route = step.get("default_route")
            if not default_route:
                dlq_log(customer_id, 500, f"DECISION_MATRIX @ {step_id} [stage:{stage_name}] has no default_route and no rule matched", context.get("_raw"))
                raise HTTPException(status_code=500, detail=f"Contract Configuration Error: DECISION_MATRIX '{step_id}' missing 'default_route'")
            matched_tag = default_route
        context["route_tag"] = matched_tag
        return step.get("next", "END")

    if st is StepType.DISPATCH:
        public_ctx = {k: v for k, v in context.items() if not k.startswith("_")}
        method = step.get("method", "POST").upper()
        try:
            url = jinja_env.from_string(step.get("url", "")).render(**public_ctx)
            headers = {
                k: jinja_env.from_string(v).render(**public_ctx) for k, v in step.get("headers", {}).items()
            }
            body = {
                target_key: jmespath.search(query, context)
                for target_key, query in step.get("body_mapping", {}).items()
            }
            resp = await http_client.request(
                method, url, json=body, headers=headers, timeout=step.get("timeout_seconds", 5)
            )
            resp.raise_for_status()
            context[step.get("result_key", "dispatch_result")] = {"status_code": resp.status_code}
        except httpx.HTTPError as e:
            dlq_log(customer_id, 502, f"HTTP_DISPATCH failed @ {step_id} [stage:{stage_name}]: {str(e)}", context.get("_raw"))
            if step.get("on_error") == "continue":
                return step.get("next", "END")
            raise HTTPException(status_code=502, detail=f"Egress Dispatch Failed @ {step_id}: {str(e)}")
        return step.get("next", "END")

    if st is StepType.RENDER:
        template_path = f"contracts/{customer_id}/logic_rule_output.j2"
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template_text = f.read()
        except FileNotFoundError:
            dlq_log(customer_id, 500, f"RENDER_ARTIFACT @ {step_id} [stage:{stage_name}]: template file missing", context.get("_raw"))
            raise HTTPException(status_code=500, detail="Contract Configuration Error: logic_rule_output.j2 not found")

        # Drop internal (_-prefixed) keys AND explicit None values so that
        # optional fields become truly Undefined to Jinja — this makes
        # `{{ field | default('N/A') }}` behave predictably regardless of
        # whether the field was absent from the payload or merely null.
        render_context = {
            k: v for k, v in context.items() if not k.startswith("_") and v is not None
        }
        render_context.setdefault("current_timestamp", datetime.now(timezone.utc).isoformat())

        try:
            rendered = jinja_env.from_string(template_text).render(**render_context)
        except Exception as e:
            dlq_log(customer_id, 500, f"RENDER_ARTIFACT @ {step_id} [stage:{stage_name}] failed: {str(e)}", context.get("_raw"))
            raise HTTPException(status_code=500, detail=f"Template Render Error: {str(e)}")

        context["_render_result"] = {"artifact": rendered, "format": step.get("output_format", "unknown")}
        return step.get("next", "END")

    # Unreachable given the StepType(...) coercion above, kept as a hard stop.
    raise HTTPException(status_code=500, detail=f"Contract Configuration Error: unhandled step type at '{step_id}'")

MAX_STEPS = 100  # defense-in-depth backstop; cycle detection below is the real guard


async def _execute_dag(stage_contract: dict, payload_dict: dict, customer_id: str, stage_name: str) -> dict:
    dag_map = stage_contract.get("dag", {})
    current_step_id = stage_contract.get("entry_step")
    context: Dict[str, Any] = {"_raw": payload_dict}
    visited: set = set()
    steps_executed = 0

    async with httpx.AsyncClient() as client:
        # _TERMINAL_MARKERS, not a bare "END" check: upload-time validation
        # (_validate_single_stage_dag) and the Sync Guard's walker already
        # accept ANY marker in _TERMINAL_MARKERS as a legal pointer target
        # -- a contract with "next": "DLQ_REJECT" passed validation but
        # 500'd here before this fix, since this loop only recognized
        # literal "END". Validation and execution now agree.
        while current_step_id and current_step_id not in _TERMINAL_MARKERS:
            if current_step_id in visited:
                dlq_log(customer_id, 500, f"Cycle detected in DAG at step '{current_step_id}' [stage:{stage_name}]", payload_dict)
                raise HTTPException(
                    status_code=500,
                    detail=f"Contract Configuration Error: cycle detected at step '{current_step_id}'",
                )
            visited.add(current_step_id)

            steps_executed += 1
            if steps_executed > MAX_STEPS:
                dlq_log(customer_id, 500, f"DAG exceeded maximum step limit [stage:{stage_name}]", payload_dict)
                raise HTTPException(status_code=500, detail="Contract Configuration Error: step limit exceeded")

            step = dag_map.get(current_step_id)
            if step is None:
                dlq_log(customer_id, 500, f"DAG references undefined step_id '{current_step_id}' [stage:{stage_name}]", payload_dict)
                raise HTTPException(
                    status_code=500,
                    detail=f"Contract Configuration Error: undefined step '{current_step_id}'",
                )

            current_step_id = await _run_step(step, current_step_id, context, customer_id, stage_name, client)

    if current_step_id == "DLQ_REJECT":
        dlq_log(customer_id, 200, f"DAG explicitly routed to DLQ_REJECT [stage:{stage_name}]", payload_dict)

    render_result = context.get("_render_result")
    if render_result is not None:
        data_payload: Any = {
            "artifact": render_result.get("artifact"),
            "format": render_result.get("format"),
        }
    else:
        # route_tag is already promoted to the envelope's top-level field
        # below — excluded here so it isn't duplicated inside `data`.
        data_payload = {k: v for k, v in context.items() if not k.startswith("_") and k != "route_tag"}

    # --- State-driven response: status/next_stage come from the STAGE's
    # own declared metadata (stage_contract), not from anything computed
    # during DAG execution — lifecycle/next_stage are pipeline config, the
    # DAG run only produces data. Four-way status distinction:
    #   REJECTED  — the DAG explicitly routed to the DLQ_REJECT terminal
    #               marker (e.g. an EVALUATE_CONDITION on_false branch) —
    #               checked first, since a contract can reach DLQ_REJECT
    #               regardless of what the stage's own next_stage/lifecycle
    #               metadata says.
    #   PENDING   — next_stage explicitly set to a real stage -> more
    #               orchestration to do, caller should call back with
    #               ?stage={{next_stage}}.
    #   COMPLETED — next_stage explicitly set to null, OR lifecycle is
    #               'finalize' -> this was deliberately the last stage.
    #   SUCCESS   — neither declared -> legacy-equivalent contract that
    #               hasn't opted into the lifecycle system; behaves
    #               exactly as before this feature existed.
    next_stage_declared = "next_stage" in stage_contract
    next_stage_value = stage_contract.get("next_stage")
    lifecycle = stage_contract.get("lifecycle")

    if current_step_id == "DLQ_REJECT":
        status = "REJECTED"
    elif next_stage_declared and next_stage_value is not None:
        status = "PENDING"
    elif (next_stage_declared and next_stage_value is None) or lifecycle == "finalize":
        status = "COMPLETED"
    else:
        status = "SUCCESS"

    return {
        "status": status,
        "customer_id": customer_id,
        "current_stage": stage_name,
        "next_stage": next_stage_value,
        "route_tag": context.get("route_tag"),
        "data": data_payload,
    }
