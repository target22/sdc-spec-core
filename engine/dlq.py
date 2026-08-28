"""
engine/dlq.py — owns the DLQ log file end-to-end: the write side
(BaseLogSink/FileLogSink/dlq_log, called from everywhere else in the
engine) and the read side (read_and_decrypt_dlq, called only by the
admin decrypt endpoint). Same file format, same module, symmetric
responsibility -- nothing else in the package parses the DLQ log's
on-disk line format.
"""
import glob
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from engine.logging_setup import app_logger, dlq_logger
from engine.security import decrypt_payload, sanitize_payload

class BaseLogSink(ABC):
    @abstractmethod
    def write(self, entry: Dict[str, Any]) -> None:
        """Persist one structured DLQ entry. Implementations decide the
        storage medium; callers only ever see this one method."""
        raise NotImplementedError


class FileLogSink(BaseLogSink):
    """Default sink: one JSON object per line, rotation-safe. Wraps the
    exact RotatingFileHandler already configured on dlq_logger above --
    this class does not change on-disk behavior, only how dlq_log() talks
    to it, so existing log files and the /logs endpoint are unaffected."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def write(self, entry: Dict[str, Any]) -> None:
        self._logger.error(json.dumps(entry, default=str, ensure_ascii=False))


# Swap this single line to change where DLQ entries go (e.g.
# `_active_log_sink = PostgresLogSink(dsn=...)`); nothing else in this
# module needs to change.
_active_log_sink: BaseLogSink = FileLogSink(dlq_logger)


def dlq_log(customer_id: str, status_code: int, reason: str, payload: Optional[dict] = None) -> None:
    """Structured DLQ entry, delegated to _active_log_sink. `payload` is
    always passed through the Value Proxy (sanitize_payload) before it
    reaches any sink -- see 0b above. `reason` is caller-built from static
    strings plus field NAMES (never field values) everywhere in this
    module except one call site (schema validation failures), which is
    handled separately by not embedding jsonschema's raw e.message — see
    the VALIDATE step handler."""
    entry = {
        "customer_id": customer_id,
        "status_code": status_code,
        "reason": reason,
        "payload": sanitize_payload(payload),
    }
    _active_log_sink.write(entry)


def read_and_decrypt_dlq(include_rotated: bool = False) -> Dict[str, Any]:
    """Reads DLQ log file(s), decrypts every AES-GCM leaf back to
    plaintext via DLQ_ENCRYPTION_KEY. Called only from behind the
    verify_dlq_admin_key dependency (see main.py) -- this function
    itself performs no auth, callers must gate access before invoking
    it. Extracted verbatim from the original dlq_decrypt endpoint body;
    the endpoint in main.py is now a two-line auth-then-call wrapper.
    """
    log_path = "logs/dlq_exceptions.log"
    paths = [log_path]
    if include_rotated:
        paths += sorted(glob.glob(f"{log_path}.*"))

    decrypted_entries: List[dict] = []
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Formatter is "%(asctime)s - %(levelname)s - %(message)s";
                # split on the first 2 " - " to recover the JSON message
                # regardless of how many " - " sequences appear inside it.
                parts = line.split(" - ", 2)
                raw_json = parts[2] if len(parts) == 3 else line
                try:
                    entry = json.loads(raw_json)
                except json.JSONDecodeError:
                    continue  # skip unparseable/foreign lines, don't 500 the whole endpoint
                entry["payload"] = decrypt_payload(entry.get("payload"))
                decrypted_entries.append(entry)

    app_logger.info(f"DLQ decrypt endpoint accessed -- {len(decrypted_entries)} entries decrypted")
    return {"count": len(decrypted_entries), "entries": decrypted_entries}
