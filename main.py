"""
main.py — DUMB ROUTER. Boots FastAPI and defines the 4 HTTP endpoints.
Zero domain logic: every endpoint body is thin orchestration over
engine/* — parse/validate/execute calls happen here, but the actual
rules for HOW to validate or execute live entirely in engine/.
"""
import json
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Path, Query, Request, UploadFile
from jinja2 import TemplateSyntaxError

from engine.boundary import _validate_canonical_keys, _validate_dag_structure, _validate_schema_mapping_sync
from engine.contracts import (
    CANONICAL_DEFAULT_STAGE,
    _adapt_contract,
    _load_dag_contract,
    _resolve_default_stage_name,
    _resolve_stage,
    persist_contract,
)
from engine.dlq import dlq_log, read_and_decrypt_dlq
from engine.executor import _execute_dag
from engine.logging_setup import app_logger
from engine.payload import _resolve_payload, _verify_contract_id_match
from engine.security import verify_admin_key, verify_dlq_admin_key
from engine.template_engine import jinja_env
from engine.transforms import StepType

app = FastAPI(title="Sovereign Core Engine — DAG Runner", docs_url="/docs")


@app.post("/v1/admin/contracts/{contract_id}", dependencies=[Depends(verify_admin_key)])
async def upload_contract(
    contract_id: str = Path(...),
    input_contract: UploadFile = File(..., description="Declarative DAG contract (logic_rule_input.json)"),
    output_contract: UploadFile = File(..., description="Jinja2 egress template (logic_rule_output.j2)"),
):
    in_content = await input_contract.read()
    out_content = await output_contract.read()

    # --- 1. logic_rule_input.json must be well-formed JSON -----------------
    try:
        dag_contract = json.loads(in_content)
    except json.JSONDecodeError as e:
        dlq_log(contract_id, 400, f"Admin Error: logic_rule_input.json invalid JSON ({str(e)})")
        raise HTTPException(status_code=400, detail=f"logic_rule_input.json is not valid JSON: {str(e)}")

    # --- 2. Normalize: legacy-shape detection + Pydantic schema validation --
    # Runs here AND on every runtime load (_load_dag_contract callers) —
    # upload-time and request-time go through the identical adapter, so
    # a contract that passes upload can never fail differently at runtime
    # purely because of shape.
    normalized = _adapt_contract(dag_contract, contract_id)

    # --- 3. The DAG itself must be structurally sound (every stage) --------
    _validate_dag_structure(normalized)

    # --- 4. Template is parsed as Jinja2, never forced through json.loads ---
    try:
        template_text = out_content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"logic_rule_output.j2 is not valid UTF-8: {str(e)}")
    try:
        jinja_env.parse(template_text)
    except TemplateSyntaxError as e:
        dlq_log(contract_id, 400, f"Admin Error: logic_rule_output.j2 syntax error ({e.message} @ line {e.lineno})")
        raise HTTPException(
            status_code=400,
            detail=f"logic_rule_output.j2 has invalid Jinja2 syntax at line {e.lineno}: {e.message}",
        )

    # --- 5. Mapping keys vs. schema boundary must be in sync (every stage) --
    _validate_schema_mapping_sync(normalized)

    # --- 6. Optional whole-contract canonical-key / route_tag drift guard --
    _validate_canonical_keys(normalized)

    # --- 7. Persist ORIGINAL bytes (contracts are the IP Vault — write only
    # after all checks pass). See engine/contracts.py:persist_contract for
    # why the normalized form is never what gets written.
    contract_dir = persist_contract(contract_id, in_content, template_text)

    stage_count = len(normalized["stages"])
    app_logger.info(
        f"[{contract_id}] Contract updated: {stage_count} stage(s), "
        f"contract_version={normalized['contract_version']}"
    )
    return {
        "status": "Contracts updated",
        "contract_id": contract_id,
        "path": contract_dir,
        "stages": stage_count,
        "contract_version": normalized["contract_version"],
    }

@app.get("/v1/admin/contracts/{contract_id}/spec", dependencies=[Depends(verify_admin_key)])
async def get_contract_spec(contract_id: str = Path(...)):
    """Directive 5 patch (Self-Describing Contracts). Everything here is
    read directly off the contract already on disk -- there is no
    separate hand-maintained doc to fall out of sync when the JSON
    changes; if the contract is wrong, this endpoint is wrong the same
    way, which is at least an honest failure mode instead of a silent
    stale one."""
    raw = _load_dag_contract(contract_id)
    normalized = _adapt_contract(raw, contract_id)

    spec: Dict[str, Any] = {
        "contract_id": contract_id,
        "contract_version": normalized["contract_version"],
        "stages": {},
    }
    for stage_name, stage in normalized["stages"].items():
        matrix_steps = [s for s in stage.get("dag", {}).values() if s.get("type") == StepType.MATRIX.value]
        route_tags = {
            rule.get("route_tag") for step in matrix_steps for rule in step.get("rules", []) if rule.get("route_tag")
        }
        route_tags |= {step.get("default_route") for step in matrix_steps if step.get("default_route")}

        validate_step = next(
            (s for s in stage.get("dag", {}).values() if s.get("type") == StepType.VALIDATE.value), None
        )
        render_step = next(
            (s for s in stage.get("dag", {}).values() if s.get("type") == StepType.RENDER.value), None
        )

        spec["stages"][stage_name] = {
            "lifecycle": stage.get("lifecycle"),
            "next_stage": stage.get("next_stage"),
            "entry_step": stage.get("entry_step"),
            "step_count": len(stage.get("dag", {})),
            "input_schema": validate_step.get("schema") if validate_step else None,
            "possible_route_tags": sorted(route_tags) if route_tags else None,
            "produces_artifact": render_step.get("output_format") if render_step else None,
        }
    return spec

@app.post("/v1/execute/{contract_id}")
async def execute(
    request: Request,
    contract_id: str = Path(...),
    stage: Optional[str] = Query(
        None,
        description=(
            "Pipeline stage key. Omit to use the contract's canonical default "
            f"('{CANONICAL_DEFAULT_STAGE}')."
        ),
    ),
    file: UploadFile = File(None, description="Upload payload JSON file"),
    json_text: str = Form(None, description="Paste raw JSON text"),
):
    payload_dict = await _resolve_payload(request, file, json_text, contract_id)
    raw_contract = _load_dag_contract(contract_id)
    contract = _adapt_contract(raw_contract, contract_id)
    _verify_contract_id_match(contract, contract_id)
    effective_stage = stage if stage is not None else _resolve_default_stage_name(contract, contract_id)
    stage_contract = _resolve_stage(contract, effective_stage, contract_id)
    return await _execute_dag(stage_contract, payload_dict, contract_id, effective_stage)


@app.get("/v1/admin/dlq/decrypt", dependencies=[Depends(verify_dlq_admin_key)])
async def dlq_decrypt(include_rotated: bool = False):
    """Reads DLQ log file(s), decrypts every AES-GCM leaf back to
    plaintext via DLQ_ENCRYPTION_KEY. Gated on X-Admin-Key
    (verify_dlq_admin_key) -- this is the single most sensitive endpoint
    in the engine, since its entire purpose is reversing the
    Zero-PII-at-rest protection for authorized debugging. Actual log
    parsing + decryption lives in engine/dlq.py:read_and_decrypt_dlq --
    this endpoint is auth + delegation only."""
    return read_and_decrypt_dlq(include_rotated=include_rotated)
