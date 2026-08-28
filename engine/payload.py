"""
engine/payload.py — checks performed on an incoming /v1/execute
request before DAG execution starts: parsing the HTTP body itself
(_resolve_payload) and confirming the URL's contract_id agrees with
the loaded contract's own declared customer_id (_verify_contract_id_match).
Deliberately kept out of main.py so the execute endpoint stays a thin
orchestration call, and out of engine/contracts.py since these two
functions are about the REQUEST, not the contract's own structure.
"""
import json
from typing import Optional

from fastapi import HTTPException, Request, UploadFile

from engine.dlq import dlq_log
from engine.logging_setup import app_logger

async def _resolve_payload(
    request: Request,
    file: Optional[UploadFile],
    json_text: Optional[str],
    customer_id: str,
) -> dict:
    content_type = request.headers.get("content-type", "")
    try:
        if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            if file and file.filename:
                content = await file.read()
                return json.loads(content)
            elif json_text:
                return json.loads(json_text)
            else:
                dlq_log(customer_id, 400, "Missing payload data (no file or json_text provided)")
                raise HTTPException(status_code=400, detail="Missing payload data")
        return await request.json()
    except json.JSONDecodeError:
        dlq_log(customer_id, 400, "Malformed JSON Injection Blocked")
        raise HTTPException(status_code=400, detail="Malformed JSON Injection Blocked")


def _verify_contract_id_match(contract: dict, url_contract_id: str) -> None:
    """Ingress Validation Check: the contract_id in the URL path must
    match the contract's own declared 'customer_id' field -- but ONLY
    when the contract declares one. A contract that never sets this field
    is not retroactively rejected; the check is opt-in per contract."""
    declared_id = contract.get("customer_id")
    if declared_id is None:
        app_logger.warning(
            f"contract_id='{url_contract_id}': loaded contract declares no internal "
            f"customer_id -- skipping match check."
        )
        return
    if declared_id != url_contract_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"contract_id mismatch: URL path declares '{url_contract_id}', but the "
                f"loaded contract's internal customer_id is '{declared_id}'."
            ),
        )
