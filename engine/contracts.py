"""
engine/contracts.py — contract structure end-to-end: the Pydantic
envelope (StageContract/NormalizedContract/LegacyFlatContract), the
legacy-vs-normalized adapter, default-stage resolution, and contract
disk I/O (load + persist). One module owns "what a contract IS and
where it lives on disk" so every other module can treat a normalized
contract as a plain dict without re-deriving any of this.
"""
import json
import os
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError as PydanticValidationError, model_validator

from engine.dlq import dlq_log
from engine.logging_setup import app_logger

os.makedirs("contracts", exist_ok=True)

CANONICAL_DEFAULT_STAGE = "init"

_SUPPORTED_CONTRACT_VERSIONS = {"v1-legacy-adapted", "v2-unversioned", "v2", "3.0"}


class StageLifecycle(str, Enum):
    INIT = "init"
    ENRICH = "enrich"
    FINALIZE = "finalize"


class StageContract(BaseModel):
    """One entry under a normalized contract's 'stages' object."""
    model_config = ConfigDict(extra="allow")

    entry_step: str
    dag: Dict[str, Any]
    lifecycle: Optional[StageLifecycle] = None
    next_stage: Optional[str] = None


class NormalizedContract(BaseModel):
    """The ONE shape every contract is guaranteed to have in memory after
    _adapt_contract runs — regardless of whether the file on disk is a
    pre-v3 flat contract, an unversioned v3 multi-stage contract, or a
    fully-versioned contract. Downstream code (_resolve_stage,
    _validate_dag_structure, _execute_dag, ...) assumes this shape
    unconditionally; the legacy/heuristic branching happens exactly once,
    in _adapt_contract, not scattered across every consumer."""
    model_config = ConfigDict(extra="allow")

    contract_version: str
    customer_id: Optional[str] = None
    stages: Dict[str, StageContract]
    # Kernel Space Isolation fix: replaces a previously hardcoded, single-
    # customer fallback stage name. A contract may explicitly opt into a
    # non-canonical default entry-stage name by declaring this field; the
    # kernel carries no assumption about what that name might be for any
    # specific contract.
    legacy_default_stage: Optional[str] = None
    # Directive 2 patch (Canonical IR): opt-in cross-stage drift guards.
    # None (the default) means "not declared" -- _validate_canonical_keys
    # skips entirely, so contracts written before this existed are
    # completely unaffected.
    canonical_keys: Optional[List[str]] = None
    canonical_route_tags: Optional[List[str]] = None

    @model_validator(mode="after")
    def _next_stage_points_somewhere_real(self):
        for stage_name, stage in self.stages.items():
            if stage.next_stage is not None and stage.next_stage not in self.stages:
                raise ValueError(
                    f"Stage '{stage_name}' declares next_stage={stage.next_stage!r}, which "
                    f"is not a stage defined in this contract. "
                    f"Available stages: {sorted(self.stages.keys())}"
                )
        return self


class LegacyFlatContract(BaseModel):
    """Shape of a pre-v3 contract: entry_step/dag live at the document
    root, no 'stages' wrapper. Validated BEFORE _adapt_contract wraps it,
    so a malformed legacy upload gets a clear Pydantic error instead of a
    downstream KeyError three functions later."""
    model_config = ConfigDict(extra="allow")

    entry_step: str
    dag: Dict[str, Any]
    customer_id: Optional[str] = None


def _adapt_contract(raw_contract: Any, customer_id: str) -> dict:
    """The one place legacy-vs-normalized branching happens. Called on
    every contract load — both /admin upload-time validation and every
    per-request runtime load — so every downstream consumer can assume
    the NormalizedContract shape unconditionally afterward.

    Two independent triggers, deliberately NOT collapsed into one 'lacks
    version OR lacks stages' check: a contract can lack a version string
    while already having 'stages' (just needs a version stamped on it),
    and conflating the two would wrap an already-staged contract under
    stages.init AGAIN, double-nesting it.

      1. No 'stages' key at all -> true pre-v3 flat contract. Wrapped into
         stages.init in RAM only; nothing is rewritten on disk.
      2. 'stages' present but no non-empty 'contract_version' -> already
         v3-shaped, just missing the version label. Stamped with a
         default version, structure untouched.
    """
    if not isinstance(raw_contract, dict):
        raise HTTPException(status_code=400, detail="Contract root must be a JSON object")

    has_stages = isinstance(raw_contract.get("stages"), dict)

    if not has_stages:
        try:
            LegacyFlatContract.model_validate(raw_contract)
        except PydanticValidationError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Contract has no 'stages' object, and doesn't match the legacy flat shape either: {e}",
            )
        app_logger.warning(
            f"[{customer_id}] DEPRECATED contract shape: no 'stages' object. Adapted in-memory to "
            f"contract_version='v1-legacy-adapted', stage='{CANONICAL_DEFAULT_STAGE}'. "
            f"The file on disk is untouched — migrate to the explicit multi-stage format "
            f"when convenient, this is not an error."
        )
        normalized: dict = {
            "contract_version": "v1-legacy-adapted",
            "customer_id": raw_contract.get("customer_id", customer_id),
            "stages": {
                CANONICAL_DEFAULT_STAGE: {
                    "entry_step": raw_contract.get("entry_step"),
                    "dag": raw_contract.get("dag"),
                    "lifecycle": "init",
                    "next_stage": None,
                }
            },
        }
    else:
        normalized = dict(raw_contract)
        version = normalized.get("contract_version")
        if not isinstance(version, str) or not version.strip():
            app_logger.warning(
                f"[{customer_id}] Contract has 'stages' but no explicit 'contract_version'; "
                f"defaulting to 'v2-unversioned'."
            )
            normalized["contract_version"] = "v2-unversioned"

    try:
        NormalizedContract.model_validate(normalized)
    except PydanticValidationError as e:
        raise HTTPException(status_code=400, detail=f"Contract failed schema validation: {e}")

    # Directive 4 patch: contract_version stops being cosmetic labeling.
    # Today every recognized version runs through the identical validate/
    # execute path -- that's still true after this gate -- but an
    # UNRECOGNIZED version is now rejected explicitly instead of silently
    # inheriting whatever the current engine happens to do. When a future
    # change needs different behavior per version, this is the seam to
    # branch on; until then this is a deliberately blunt allowlist, not
    # speculative multi-version execution logic for versions that don't
    # exist yet.
    if normalized["contract_version"] not in _SUPPORTED_CONTRACT_VERSIONS:
        error_msg = (
            f"Unrecognized contract_version '{normalized['contract_version']}'. "
            f"Supported: {sorted(_SUPPORTED_CONTRACT_VERSIONS)}"
        )
        dlq_log(customer_id, 400, error_msg)
        raise HTTPException(status_code=400, detail=error_msg)

    return normalized

def _resolve_default_stage_name(contract: dict, customer_id: str) -> str:
    """Picks the stage to run when ?stage= is omitted. `contract` must
    already be normalized (has 'stages'). Resolution order:
      1. literal 'init' key — the canonical default for every contract.
      2. the contract's own declared 'legacy_default_stage' field, if
         present and if it names a stage that actually exists. This is an
         EXPLICIT, per-contract opt-in — the kernel carries no assumption
         about what any specific contract's non-canonical entry stage is
         named; that knowledge lives entirely in the contract file itself.
      3. neither resolves -> 400, caller must pass ?stage= explicitly.
    """
    stages = contract["stages"]
    if CANONICAL_DEFAULT_STAGE in stages:
        return CANONICAL_DEFAULT_STAGE
    legacy_default = contract.get("legacy_default_stage")
    if legacy_default and legacy_default in stages:
        return legacy_default
    error_msg = (
        f"Contract for customer '{customer_id}' has no '{CANONICAL_DEFAULT_STAGE}' stage, and "
        f"either declares no 'legacy_default_stage' or names one that does not exist; ?stage= "
        f"must be specified explicitly. Available stages: {sorted(stages.keys())}"
    )
    dlq_log(customer_id, 400, error_msg)
    raise HTTPException(status_code=400, detail=error_msg)

def _load_dag_contract(customer_id: str) -> dict:
    input_path = f"contracts/{customer_id}/logic_rule_input.json"
    template_path = f"contracts/{customer_id}/logic_rule_output.j2"
    # Check both files up front, before executing any step — including any
    # HTTP_DISPATCH side effect — so we never fire an egress call for a
    # customer whose render template is missing.
    if not os.path.exists(input_path) or not os.path.exists(template_path):
        error_msg = f"Contract Configs Missing for this Customer ({customer_id})"
        dlq_log(customer_id, 404, error_msg)
        raise HTTPException(status_code=404, detail=error_msg)
    with open(input_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _resolve_stage(contract: dict, stage_name: str, customer_id: str) -> dict:
    """Extracts the stage sub-contract (entry_step, dag, and whatever
    lifecycle/next_stage metadata it carries) for the requested pipeline
    stage. `contract` must already be normalized (run through
    _adapt_contract) — 'stages' is assumed present unconditionally, so
    this function has exactly one job: does the requested key exist."""
    stages = contract["stages"]
    stage = stages.get(stage_name)
    if stage is None:
        error_msg = (
            f"Stage '{stage_name}' is not defined for customer '{customer_id}'. "
            f"Available stages: {sorted(stages.keys())}"
        )
        dlq_log(customer_id, 404, error_msg)
        raise HTTPException(status_code=404, detail=error_msg)
    return stage


def persist_contract(contract_id: str, in_content: bytes, template_text: str) -> str:
    """Writes a validated contract's ORIGINAL bytes to disk (contracts
    are the IP Vault — write only after every upload-time check has
    passed). The normalized form is never written: it's an in-memory
    convenience recomputed on every load, so a legacy file re-uploaded
    later still diffs cleanly against what's on disk. Extracted
    verbatim from the original upload_contract endpoint body (step 7);
    returns the contract_dir path for the endpoint's response. Caller
    (main.py) is responsible for having already run every validation
    step in _validate_dag_structure / _validate_schema_mapping_sync /
    _validate_canonical_keys before calling this.
    """
    contract_dir = f"contracts/{contract_id}"
    os.makedirs(contract_dir, exist_ok=True)
    with open(f"{contract_dir}/logic_rule_input.json", "wb") as f:
        f.write(in_content)
    with open(f"{contract_dir}/logic_rule_output.j2", "w", encoding="utf-8") as f:
        f.write(template_text)
    return contract_dir
