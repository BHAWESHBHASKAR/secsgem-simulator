"""Logging configuration: readable text (default) or one-JSON-object-per-line.

Message traces from the HSMS layer attach structured fields via `extra=`
(direction, sxfy, system_bytes, ...). The JSON formatter emits them as keys so
logs can be shipped to a log pipeline and queried, e.g. "all S5F1 in the last hour".
"""

from __future__ import annotations

import json
import logging
import sys

STRUCTURED_FIELDS = ("direction", "sxfy", "wbit", "control", "system_bytes", "peer")


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s.%(msecs)03d %(levelname)-7s %(short)-10s %(message)s",
                         datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.short = record.name.split(".")[-1]
        return super().format(record)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in STRUCTURED_FIELDS:
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(level: str = "INFO", fmt: str = "text", trace: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    if not trace:
        logging.getLogger("secsgem_sim.trace").setLevel(logging.WARNING)
