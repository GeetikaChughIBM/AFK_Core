"""
observability.py — Structured logging and observability for the Software Factory.

All factory components use get_logger() from this module.
Logs are written to:
  - stderr (human-readable, colourised via Rich)
  - logs/factory.jsonl (machine-readable JSONL for audit/replay)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

_console = Console(stderr=True)
_jsonl_fh: Optional[object] = None
_log_dir: Optional[Path] = None


def init_logging(log_dir: str = "logs", log_level: str = "INFO") -> None:
    """
    Initialise the factory-wide logging subsystem.
    Call once from main.py before anything else.
    """
    global _jsonl_fh, _log_dir

    _log_dir = Path(log_dir)
    _log_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = _log_dir / "factory.jsonl"
    _jsonl_fh = open(jsonl_path, "a", encoding="utf-8")

    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=_console,
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            )
        ],
    )
    # Suppress noisy third-party loggers
    logging.getLogger("paramiko").setLevel(logging.WARNING)


def get_logger(name: str) -> "StructuredLogger":
    """Return a StructuredLogger wrapping the named stdlib logger."""
    return StructuredLogger(name)


class StructuredLogger:
    """
    A thin wrapper around stdlib Logger that also emits JSONL records for
    machine-readable audit trails and structured observability.
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._name = name

    # ── Public API ────────────────────────────────────────────────────────────

    def info(self, msg: str, **ctx) -> None:
        self._emit("INFO", msg, **ctx)

    def debug(self, msg: str, **ctx) -> None:
        self._emit("DEBUG", msg, **ctx)

    def warning(self, msg: str, **ctx) -> None:
        self._emit("WARNING", msg, **ctx)

    def error(self, msg: str, **ctx) -> None:
        self._emit("ERROR", msg, **ctx)

    def critical(self, msg: str, **ctx) -> None:
        self._emit("CRITICAL", msg, **ctx)

    def event(self, event_type: str, msg: str, **ctx) -> None:
        """
        Log a structured factory lifecycle event.

        event_type examples:
          WORKER_STATE_CHANGE, TASK_STATE_CHANGE, GATE_PASS,
          GATE_FAIL, ARTIFACT_PRODUCED, BUILD_STARTED, BUILD_COMPLETE,
          SSH_COMMAND, GIT_PUSH, HUMAN_APPROVAL_REQUESTED, MERGED
        """
        ctx["event_type"] = event_type
        self._emit("INFO", msg, **ctx)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _emit(self, level: str, msg: str, **ctx) -> None:
        stdlib_level = getattr(logging, level, logging.INFO)
        # Human-readable log via Rich
        context_str = "  ".join(f"{k}={v}" for k, v in ctx.items())
        display = f"{msg}  {context_str}" if context_str else msg
        self._logger.log(stdlib_level, display)

        # Machine-readable JSONL
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "logger": self._name,
            "msg": msg,
            **ctx,
        }
        if _jsonl_fh is not None:
            try:
                _jsonl_fh.write(json.dumps(record) + "\n")
                _jsonl_fh.flush()
            except Exception:
                pass  # Never let logging kill the factory
