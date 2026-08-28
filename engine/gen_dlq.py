"""
Generates a synthetic logs/dlq_exceptions.log fixture that is byte-accurate
to the REAL engine/security.py::sanitize_payload -> _encrypt_value pipeline
and the REAL engine/logging_setup.py Formatter line shape -- not a mockup.

Uses a throwaway demo key (printed below, NOT a real DLQ_ENCRYPTION_KEY).
Field names are the domain-neutral SDC-SPEC-INGRESS-v3.1.json reference
contract fields (record_id/status/owner_id/...), not real client data.
"""
import base64, hashlib, json, secrets
from datetime import datetime, timedelta
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- mirrors engine/security.py exactly (function-for-function) ----------
DEMO_KEY = secrets.token_bytes(32)  # throwaway -- NOT a real production key
_AESGCM = AESGCM(DEMO_KEY)
_ENC_MAX_DEPTH = 12

def _fingerprint(raw: str) -> str:                              # security.py:_fingerprint
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

def _encrypt_str(raw: str):                                      # security.py:_encrypt_str
    nonce = secrets.token_bytes(12)
    ciphertext = _AESGCM.encrypt(nonce, raw.encode("utf-8"), associated_data=None)
    return {"__enc__": "aesgcm", "n": base64.b64encode(nonce).decode("ascii"),
            "c": base64.b64encode(ciphertext).decode("ascii")}

def _encrypt_value(value, depth=0):                               # security.py:_encrypt_value
    if depth >= _ENC_MAX_DEPTH:
        return "<max-depth-exceeded>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        raw = str(value)
        return {"type": type(value).__name__, "fp": _fingerprint(raw), **_encrypt_str(raw)}
    if isinstance(value, str):
        if not value:
            return ""
        return {"type": "str", "len": len(value), "fp": _fingerprint(value), **_encrypt_str(value)}
    if isinstance(value, list):
        return [_encrypt_value(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _encrypt_value(v, depth + 1) for k, v in value.items()}
    return f"<{type(value).__name__}>"

def _decrypt_str(enc):
    nonce = base64.b64decode(enc["n"]); ct = base64.b64decode(enc["c"])
    return _AESGCM.decrypt(nonce, ct, associated_data=None).decode("utf-8")

def _decrypt_value(value):                                        # security.py:_decrypt_value
    if isinstance(value, dict):
        if "__enc__" in value and "n" in value and "c" in value:
            try:
                plaintext = _decrypt_str(value)
            except Exception as e:
                return f"<decrypt-failed: {e}>"
            vtype = value.get("type")
            if vtype == "int":
                try: return int(plaintext)
                except ValueError: return plaintext
            if vtype == "float":
                try: return float(plaintext)
                except ValueError: return plaintext
            return plaintext
        return {k: _decrypt_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decrypt_value(v) for v in value]
    return value

def sanitize_payload(payload):                                    # security.py:sanitize_payload
    if payload is None:
        return None
    return _encrypt_value(payload)

def dlq_log(customer_id, status_code, reason, payload=None):      # dlq.py:dlq_log
    return {"customer_id": customer_id, "status_code": status_code, "reason": reason,
            "payload": sanitize_payload(payload)}

# --- synthetic scenarios, domain-neutral reference-contract fields --------
CONTRACT_ID = "demo_reference_contract"
base_time = datetime(2026, 8, 27, 9, 14, 2)

scenarios = [
    (422,
     "Schema Violation @ step_2_validate [stage:init] field='label' validator='minLength' expected=1 got_type=str",
     {"data": {"id": "rec_48213", "status": "OPEN", "owner_id": "u_2201",
               "instance_code": "INST-2026-0092", "fields": [
                   {"name": "label", "value": ""},
                   {"name": "amount", "value": 1250.50}]}}),
    (400,
     "Malformed JSON Injection Blocked",
     None),
    (500,
     "DECISION_MATRIX rule #2 @ step_b_matrix [stage:classify] invalid: Undefined variable 'found_recordx' in condition",
     {"trigger": {"status": "CLOSED", "label": "Q3 Renewal"}, "db_check": {"found_count": 1}}),
    (200,
     "DAG explicitly routed to DLQ_REJECT [stage:classify]",
     {"trigger": {"status": "CANCELLED", "label": "Duplicate entry"}, "db_check": {"found_count": 1}}),
    (404,
     "Contract Configs Missing for this Customer (demo_reference_contract_v2)",
     None),
]

lines = []
for i, (code, reason, payload) in enumerate(scenarios):
    entry = dlq_log(CONTRACT_ID, code, reason, payload)
    ts = (base_time + timedelta(minutes=i * 7, seconds=i * 13)).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    line = f"{ts} - ERROR - {json.dumps(entry, ensure_ascii=False)}"
    lines.append(line)

with open("/mnt/user-data/outputs/dlq_exceptions.log", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

print(f"Demo key (base64, throwaway, NOT for production use): {base64.b64encode(DEMO_KEY).decode()}")
print(f"Wrote {len(lines)} entries")

# --- round-trip verification against the REAL read_and_decrypt_dlq parsing logic ---
decrypted_entries = []
with open("/mnt/user-data/outputs/dlq_exceptions.log", "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        parts = line.split(" - ", 2)                              # dlq.py:read_and_decrypt_dlq
        raw_json = parts[2] if len(parts) == 3 else line
        try:
            e = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        e["payload"] = _decrypt_value(e.get("payload"))
        decrypted_entries.append(e)

print(f"\nRound-trip check: {len(decrypted_entries)}/{len(scenarios)} entries parsed + decrypted OK")
for e in decrypted_entries:
    print(" -", e["status_code"], e["reason"][:60], "| payload recovered:", e["payload"] is not None or e["payload"] == {})
