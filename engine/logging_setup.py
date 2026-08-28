"""
engine/logging_setup.py — the two module-level logger singletons every
other module reads from. Zero dependencies on anything else in this
package, by design: it must be importable first, before security.py,
dlq.py, or anything that logs through app_logger/dlq_logger exists.
"""
import logging
import logging.handlers
import os

os.makedirs("logs", exist_ok=True)

# --- Rotating DLQ log ------------------------------------------------------
dlq_logger = logging.getLogger("sce.dlq")
dlq_logger.setLevel(logging.ERROR)
_dlq_handler = logging.handlers.RotatingFileHandler(
    filename="logs/dlq_exceptions.log",
    maxBytes=10 * 1024 * 1024,  # 10MB per file
    backupCount=5,  # dlq_exceptions.log.1 .. .5
    encoding="utf-8",
)
_dlq_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
dlq_logger.addHandler(_dlq_handler)
dlq_logger.propagate = False

# --- General application log -> stdout (for `docker logs`) ---------------
app_logger = logging.getLogger("sce.app")
app_logger.setLevel(logging.INFO)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
app_logger.addHandler(_stream_handler)
app_logger.propagate = False
